"""Files-tool tests (Patch #12) — the first coverage this surface has had.

The headline: `name_contains` used to be forwarded to Kiteworks as the `name`
query param, which the server treats as EXACT-match — every real substring
query returned 0 rows. The filter is now client-side; these tests pin the
subset invariant, case-insensitivity, and that the bare `name` param is never
sent again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from pydantic import SecretStr

from bpd_mcp.auth import AuthManager, TokenBundle
from bpd_mcp.client import KiteworksClient
from bpd_mcp.config import Settings
from bpd_mcp.schemas import ListFolderContentsInput
from bpd_mcp.tools.files import list_folder_contents

BASE = "https://securesharek.target.com"

_CHILDREN = [
    {"id": "A", "name": "BV_139440_WEEKLY_SALES_TCIN_LOC_07252026_KW.zip", "type": "f"},
    {"id": "B", "name": "BV_139440_WEEKLY_SALES_TCIN_LOC_08012026_KW.zip", "type": "f"},
    {"id": "C", "name": "BV_139440_DAILY_ORDER_TCIN_LOC_08012026_KW.zip", "type": "f"},
]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kiteworks_base_url=BASE,
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        kiteworks_client_id="cid",
        kiteworks_client_secret=SecretStr("csec"),
        bpd_data_dir=str(tmp_path),
        bpd_vendor_id="139440",
    )


def _bundle() -> TokenBundle:
    return TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _mock_children() -> None:
    respx.post(f"{BASE}/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "*/*/*",
            },
        )
    )
    respx.get(f"{BASE}/rest/folders/F1/children").mock(
        return_value=httpx.Response(200, json=_CHILDREN)
    )


async def _call(tmp_path: Path, **kwargs) -> tuple:
    s = _settings(tmp_path)
    async with httpx.AsyncClient() as http:
        auth = AuthManager(s, http, bundle=_bundle())
        client = KiteworksClient(s, auth, http)
        resp = await list_folder_contents(
            client,
            ListFolderContentsInput(folder_id="F1", response_format="json", **kwargs),
        )
    return resp


@respx.mock
async def test_name_contains_filters_client_side_case_insensitive(
    tmp_path: Path,
) -> None:
    _mock_children()
    unfiltered = await _call(tmp_path)
    assert unfiltered.ok is True
    all_ids = {i["id"] for i in unfiltered.data["items"]}
    assert all_ids == {"A", "B", "C"}

    filtered = await _call(tmp_path, name_contains="weekly_sales")
    assert filtered.ok is True
    ids = {i["id"] for i in filtered.data["items"]}
    assert ids == {"A", "B"}, "case-insensitive substring must match both weekly files"
    assert ids < all_ids, "filtered results must be a strict subset of unfiltered"

    none = await _call(tmp_path, name_contains="NOPE_NO_MATCH")
    assert none.ok is True
    assert none.data["items"] == []

    # The exact-match `name` query param must never be sent again — that's the
    # bug (Kiteworks treats it as exact-match, so substring queries got 0 rows).
    for call in respx.calls:
        url = urlparse(str(call.request.url))
        if url.path.endswith("/children"):
            assert "name" not in parse_qs(url.query), (
                "client must not forward name_contains as the server-side "
                "`name` param"
            )
