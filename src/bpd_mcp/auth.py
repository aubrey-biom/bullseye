"""OAuth2 password/refresh token manager for Kiteworks.

Kiteworks OAuth token endpoint per their developer docs: `{base_url}/oauth/token`.
The PDF supplied with the project (Step 6) re-states the same credentials and grant model.

If the endpoint ever 404s, the caller should surface the exact error verbatim — never
silently probe alternate paths (§19).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .logging_setup import get_logger, mask_token

logger = get_logger(__name__)

# Refresh slightly before expiry so a request never races against expiration.
_REFRESH_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    """Raised when both refresh and password grant fail or are impossible."""


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str = ""
    saved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self, skew_seconds: int = _REFRESH_SKEW_SECONDS) -> bool:
        return datetime.now(UTC) >= (self.expires_at - timedelta(seconds=skew_seconds))

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat(),
                "scope": self.scope,
                "saved_at": self.saved_at.isoformat(),
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenBundle:
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            scope=data.get("scope", ""),
            saved_at=datetime.fromisoformat(data.get("saved_at", datetime.now(UTC).isoformat())),
        )


def _check_perms(path: Path) -> None:
    """Refuse to start if the token file has perms looser than 0600.

    POSIX semantics. On Windows we no-op.
    """
    if sys.platform.startswith("win"):
        return
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    # Reject any group/other access.
    if mode & 0o077:
        raise AuthError(
            f"Token file {path} has insecure permissions {oct(mode)}. "
            f"Run: chmod 600 {path}"
        )


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    if not sys.platform.startswith("win"):
        os.chmod(path, 0o600)


class AuthManager:
    """Hold and refresh OAuth tokens. Single-flight refresh via an asyncio.Lock."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        *,
        bundle: TokenBundle | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client
        self._bundle: TokenBundle | None = bundle
        self._lock = asyncio.Lock()

    # ----- file persistence -----

    @classmethod
    def load_from_disk(cls, settings: Settings, http_client: httpx.AsyncClient) -> AuthManager:
        bundle: TokenBundle | None = None
        path = settings.token_file
        if path.exists():
            _check_perms(path)
            try:
                bundle = TokenBundle.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("token_file_unreadable", path=str(path), error=str(e))
                bundle = None
        return cls(settings, http_client, bundle=bundle)

    def persist(self) -> None:
        if self._bundle is None:
            return
        _atomic_write(self._settings.token_file, self._bundle.to_json())

    @property
    def bundle(self) -> TokenBundle | None:
        return self._bundle

    # ----- OAuth -----

    @property
    def _token_url(self) -> str:
        return f"{self._settings.base_url}/oauth/token"

    async def _post_token(self, data: dict[str, str]) -> TokenBundle:
        """POST the OAuth body and parse the response. Raises AuthError on failure."""
        try:
            resp = await self._http.post(
                self._token_url,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            raise AuthError(f"OAuth network error contacting {self._token_url}: {e}") from e

        if resp.status_code >= 400:
            # Surface the exact server response verbatim — never silently swap endpoints (§19).
            raise AuthError(
                f"OAuth {data['grant_type']} failed: HTTP {resp.status_code} "
                f"from {self._token_url}: {resp.text}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise AuthError(f"OAuth response was not JSON: {resp.text!r}") from e

        try:
            expires_in = int(payload["expires_in"])
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
            bundle = TokenBundle(
                access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token"),
                expires_at=expires_at,
                scope=payload.get("scope", ""),
            )
        except KeyError as e:
            raise AuthError(f"OAuth response missing field {e}: {payload!r}") from e

        logger.info(
            "oauth_token_acquired",
            grant_type=data["grant_type"],
            scope=bundle.scope,
            expires_in=expires_in,
            access_token=mask_token(bundle.access_token),
        )
        return bundle

    async def password_grant(self) -> TokenBundle:
        s = self._settings
        if not s.kiteworks_username or not s.kiteworks_password:
            raise AuthError(
                "Password grant requires KITEWORKS_USERNAME and KITEWORKS_PASSWORD. "
                "Run `bpd-bootstrap` once to seed a refresh token."
            )
        body = {
            "client_id": s.kiteworks_client_id,
            "client_secret": s.kiteworks_client_secret.get_secret_value(),
            "grant_type": "password",
            "username": s.kiteworks_username,
            "password": s.kiteworks_password.get_secret_value(),
            "scope": s.kiteworks_oauth_scope,
            "redirect_uri": f"{s.base_url}/rest/callback.html",
        }
        bundle = await self._post_token(body)
        self._bundle = bundle
        self.persist()
        return bundle

    async def refresh_grant(self) -> TokenBundle:
        if self._bundle is None or not self._bundle.refresh_token:
            raise AuthError("No refresh token on hand; password grant required.")
        s = self._settings
        body = {
            "client_id": s.kiteworks_client_id,
            "client_secret": s.kiteworks_client_secret.get_secret_value(),
            "grant_type": "refresh_token",
            "refresh_token": self._bundle.refresh_token,
        }
        bundle = await self._post_token(body)
        # Kiteworks may issue a fresh refresh token or echo the existing one — preserve either.
        if not bundle.refresh_token:
            bundle.refresh_token = self._bundle.refresh_token
        self._bundle = bundle
        self.persist()
        return bundle

    # ----- public API -----

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._bundle is None:
                logger.info("oauth_initial_login")
                await self.password_grant()
                assert self._bundle is not None
                return self._bundle.access_token

            if not self._bundle.is_expired():
                return self._bundle.access_token

            logger.info("oauth_refresh", reason="expired_or_near_expiry")
            try:
                await self.refresh_grant()
            except AuthError as e:
                logger.warning("oauth_refresh_failed", error=str(e))
                # Fallback to password grant ONCE.
                await self.password_grant()
            assert self._bundle is not None
            return self._bundle.access_token

    async def force_refresh(self) -> str:
        """Force-discard the current access token and request a fresh one."""
        async with self._lock:
            if self._bundle is None:
                await self.password_grant()
            else:
                try:
                    await self.refresh_grant()
                except AuthError:
                    await self.password_grant()
            assert self._bundle is not None
            return self._bundle.access_token

    def status(self) -> dict[str, Any]:
        if self._bundle is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "expires_at": self._bundle.expires_at.isoformat(),
            "expires_in_s": max(
                0, int((self._bundle.expires_at - datetime.now(UTC)).total_seconds())
            ),
            "scope": self._bundle.scope,
            "access_token": mask_token(self._bundle.access_token),
            "refresh_token": mask_token(self._bundle.refresh_token),
        }
