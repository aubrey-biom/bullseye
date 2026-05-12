"""Auth manager tests — mock the OAuth endpoint with respx."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from bpd_mcp.auth import AuthError, AuthManager, TokenBundle
from bpd_mcp.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="user@example.com",
        kiteworks_password=SecretStr("p@ss"),
        kiteworks_client_id="cid",
        kiteworks_client_secret=SecretStr("csec"),
        bpd_data_dir=str(tmp_path),
    )


@respx.mock
async def test_password_grant_persists_token_0600(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()
    respx.post("https://securesharek.target.com/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT-abcdef-123",
                "refresh_token": "RT-zyxwv-987",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "*/*/*",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        mgr = AuthManager(s, http)
        bundle = await mgr.password_grant()
    assert bundle.access_token == "AT-abcdef-123"
    assert s.token_file.exists()
    if not sys.platform.startswith("win"):
        mode = stat.S_IMODE(s.token_file.stat().st_mode)
        assert mode == 0o600


@respx.mock
async def test_refresh_then_password_fallback(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()

    route = respx.post("https://securesharek.target.com/oauth/token")
    # First call (refresh) -> 401, second call (password) -> 200.
    route.side_effect = [
        httpx.Response(401, json={"error": "invalid_grant"}),
        httpx.Response(
            200,
            json={
                "access_token": "AT-fresh",
                "refresh_token": "RT-fresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "*/*/*",
            },
        ),
    ]

    from datetime import UTC, datetime, timedelta

    expired = TokenBundle(
        access_token="AT-old",
        refresh_token="RT-old",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    async with httpx.AsyncClient() as http:
        mgr = AuthManager(s, http, bundle=expired)
        token = await mgr.get_access_token()
    assert token == "AT-fresh"
    assert route.call_count == 2


@respx.mock
async def test_get_access_token_uses_cached_when_fresh(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()
    route = respx.post("https://securesharek.target.com/oauth/token")

    from datetime import UTC, datetime, timedelta

    fresh = TokenBundle(
        access_token="AT-fresh",
        refresh_token="RT-fresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as http:
        mgr = AuthManager(s, http, bundle=fresh)
        token = await mgr.get_access_token()
    assert token == "AT-fresh"
    assert route.call_count == 0  # no network call when token still valid


@respx.mock
async def test_oauth_error_surfaces_verbatim(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()
    body_text = '{"error":"unsupported_grant_type","error_description":"unknown grant"}'
    respx.post("https://securesharek.target.com/oauth/token").mock(
        return_value=httpx.Response(400, text=body_text, headers={"content-type": "application/json"})
    )
    async with httpx.AsyncClient() as http:
        mgr = AuthManager(s, http)
        with pytest.raises(AuthError) as ei:
            await mgr.password_grant()
    msg = str(ei.value)
    # The exact server text must be in the surfaced message — never silently retry on alt path.
    assert "unsupported_grant_type" in msg
    assert "/oauth/token" in msg


def test_token_file_refuses_loose_permissions(tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("POSIX-only test")
    s = _settings(tmp_path)
    s.ensure_dirs()
    p = s.token_file
    p.write_text(
        json.dumps(
            {
                "access_token": "x",
                "refresh_token": "y",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "scope": "",
                "saved_at": "2025-01-01T00:00:00+00:00",
            }
        )
    )
    os.chmod(p, 0o644)
    with pytest.raises(AuthError):
        # Lazy-import via a fresh http client just to satisfy the API.
        import asyncio

        async def _bad():
            async with httpx.AsyncClient() as http:
                AuthManager.load_from_disk(s, http)

        asyncio.run(_bad())


def test_token_bundle_expiry_math() -> None:
    from datetime import UTC, datetime, timedelta

    b = TokenBundle(
        access_token="x",
        refresh_token="y",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    # With default 60s skew, a 30s remaining token is treated as expired.
    assert b.is_expired() is True
    b2 = TokenBundle(
        access_token="x",
        refresh_token="y",
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    assert b2.is_expired() is False
