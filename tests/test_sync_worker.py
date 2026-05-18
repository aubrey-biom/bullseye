"""Sync worker integration test: mock the Kiteworks API, verify end-to-end load."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import respx
from pydantic import SecretStr

from bpd_mcp.auth import AuthManager, TokenBundle
from bpd_mcp.client import KiteworksClient
from bpd_mcp.config import Settings
from bpd_mcp.sync import sync_new_files
from bpd_mcp.warehouse import Warehouse


def _zip_bytes(filename: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, body)
    return buf.getvalue()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        kiteworks_client_id="cid",
        kiteworks_client_secret=SecretStr("csec"),
        bpd_data_dir=str(tmp_path),
        bpd_vendor_id="999000",
        bpd_max_parallel_downloads=2,
    )


@respx.mock
async def test_sync_new_files_full_path(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()

    # --- Stub OAuth token endpoint.
    respx.post("https://securesharek.target.com/oauth/token").mock(
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

    # --- /rest/folders/top → one folder named "999000".
    vendor_folder = {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "999000",
        "type": "d",
        "parentId": None,
    }
    respx.get("https://securesharek.target.com/rest/folders/top").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [vendor_folder, {"id": "x", "name": "other", "type": "d"}],
                "metadata": {"total": 2, "limit": 200, "offset": 0},
            },
        )
    )

    # --- Children: one zip file matching the May 2026 DAILY_SALES_TCIN_LOC pattern.
    file_id = "00000000-0000-0000-0000-0000000000aa"
    file_name = "BV_999000_DAILY_SALES_TCIN_LOC_04212026_KW.zip"
    respx.get(
        "https://securesharek.target.com/rest/folders/"
        "00000000-0000-0000-0000-000000000001/children"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": file_id,
                    "name": file_name,
                    "type": "f",
                    "parentId": vendor_folder["id"],
                    "size": 1024,
                    "fingerprint": "abc123",
                }
            ],
        )
    )

    # --- File download: pipe-delimited zip with a few rows.
    body = (
        "TCIN|LOCATION ID|SALE DATE|UNITS SOLD|SALES DOLLARS\n"
        "100|1234|2026-04-21|5|10.0\n"
        "100|5678|2026-04-21|3|6.0\n"
        "200|1234|2026-04-21|1|2.0\n"
    )
    zb = _zip_bytes(file_name.replace(".zip", ".txt"), body)
    respx.get(f"https://securesharek.target.com/rest/files/{file_id}/content").mock(
        return_value=httpx.Response(200, content=zb)
    )

    # --- Drive a sync.
    from datetime import UTC, datetime, timedelta

    bundle = TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as http:
        auth = AuthManager(s, http, bundle=bundle)
        client = KiteworksClient(s, auth, http)
        wh = Warehouse(s.db_path)
        try:
            result = await sync_new_files(client, wh, s, triggered_by="test")
        finally:
            wh.close()

    assert result.files_loaded == 1, result
    assert result.files_failed == 0
    # Verify the warehouse actually has the rows.
    wh2 = Warehouse(s.db_path)
    try:
        _, rows = wh2.execute_sql("SELECT SUM(units_sold) FROM sales_daily")
        assert rows[0][0] == 9  # 5 + 3 + 1
        _, ledger = wh2.execute_sql(
            "SELECT status, file_name FROM _file_ledger WHERE status = 'loaded'"
        )
        assert len(ledger) == 1
        assert ledger[0][1] == file_name
    finally:
        wh2.close()


@respx.mock
async def test_sync_failed_load_preserves_prior_registry(
    tmp_path: Path, monkeypatch
) -> None:
    """Patch #6 contract. If `upsert_dataframe` raises, `register_schema` must
    NOT run, so the registry keeps reflecting the last successful load instead
    of the failed attempt's inferred types.
    """
    s = _settings(tmp_path)
    s.ensure_dirs()
    # Pre-seed warehouse with the prior "successful" schema (the types we want
    # the registry to retain even after the failed load).
    seeded = Warehouse(s.db_path)
    seeded_cols = {
        "tcin": "BIGINT",
        "location_id": "BIGINT",
        "sale_date": "DATE",
        "units_sold": "BIGINT",
        "sales_dollars": "DOUBLE",
    }
    try:
        seeded.register_schema(
            "sales_daily", seeded_cols, ("tcin", "location_id", "sale_date")
        )
        seeded.ensure_data_table("sales_daily", seeded_cols)
    finally:
        seeded.close()

    # Force upsert_dataframe to raise so we exercise the failure path.
    from bpd_mcp import warehouse as warehouse_mod

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated upsert failure")

    monkeypatch.setattr(warehouse_mod.Warehouse, "upsert_dataframe", _raise)

    respx.post("https://securesharek.target.com/oauth/token").mock(
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
    vendor_folder = {"id": "F1", "name": "999000", "type": "d"}
    respx.get("https://securesharek.target.com/rest/folders/top").mock(
        return_value=httpx.Response(
            200, json={"data": [vendor_folder], "metadata": {"total": 1}}
        )
    )
    file_id = "BAD"
    file_name = "BV_999000_DAILY_SALES_TCIN_LOC_04212026_KW.zip"
    respx.get("https://securesharek.target.com/rest/folders/F1/children").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": file_id,
                    "name": file_name,
                    "type": "f",
                    "parentId": "F1",
                    "size": 100,
                    "fingerprint": "fp-bad",
                }
            ],
        )
    )
    # An OK-shaped body. Failure comes from the monkeypatched upsert.
    body = (
        "TCIN|LOCATION ID|SALE DATE|UNITS SOLD|SALES DOLLARS|EXTRA_NEW_COL\n"
        "100|1234|2026-04-21|5|10.0|abc\n"
    )
    respx.get(f"https://securesharek.target.com/rest/files/{file_id}/content").mock(
        return_value=httpx.Response(200, content=_zip_bytes("x.txt", body))
    )

    from datetime import UTC, datetime, timedelta

    bundle = TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as http:
        auth = AuthManager(s, http, bundle=bundle)
        client = KiteworksClient(s, auth, http)
        wh = Warehouse(s.db_path)
        try:
            result = await sync_new_files(client, wh, s, triggered_by="t-bad")
        finally:
            wh.close()
    assert result.files_failed == 1
    assert result.files_loaded == 0

    # Registry must still reflect the seeded schema (no `extra_new_col`),
    # because register_schema was NOT called for the failed attempt.
    wh2 = Warehouse(s.db_path)
    try:
        schema = wh2.get_schema("sales_daily")
        assert schema is not None
        assert "extra_new_col" not in schema["columns"]
        assert set(schema["columns"]) == set(seeded_cols)
    finally:
        wh2.close()


@respx.mock
async def test_sync_is_idempotent(tmp_path: Path) -> None:
    """Re-running sync with same fingerprint must skip — row count unchanged."""
    s = _settings(tmp_path)
    s.ensure_dirs()
    respx.post("https://securesharek.target.com/oauth/token").mock(
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
    vendor_folder = {"id": "F1", "name": "999000", "type": "d"}
    respx.get("https://securesharek.target.com/rest/folders/top").mock(
        return_value=httpx.Response(200, json={"data": [vendor_folder], "metadata": {"total": 1}})
    )
    file_id = "FF"
    file_name = "BV_999000_WEEKLY_GM_TCIN_LOC_04252026_KW.zip"
    respx.get("https://securesharek.target.com/rest/folders/F1/children").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": file_id,
                    "name": file_name,
                    "type": "f",
                    "parentId": "F1",
                    "size": 500,
                    "fingerprint": "fp-1",
                }
            ],
        )
    )
    body = "TCIN|WEEK_END_DATE|GROSS_MARGIN\n1|2026-04-25|0.30\n2|2026-04-25|0.40\n"
    zb = _zip_bytes("x.txt", body)
    respx.get(f"https://securesharek.target.com/rest/files/{file_id}/content").mock(
        return_value=httpx.Response(200, content=zb)
    )

    from datetime import UTC, datetime, timedelta

    bundle = TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as http:
        auth = AuthManager(s, http, bundle=bundle)
        client = KiteworksClient(s, auth, http)
        wh = Warehouse(s.db_path)
        try:
            r1 = await sync_new_files(client, wh, s, triggered_by="t1")
            r2 = await sync_new_files(client, wh, s, triggered_by="t2")
        finally:
            wh.close()
    assert r1.files_loaded == 1
    assert r2.files_loaded == 0
    assert r2.files_skipped == 1
    # Verify row count unchanged after the second sync.
    wh2 = Warehouse(s.db_path)
    try:
        _, rows = wh2.execute_sql("SELECT COUNT(*) FROM gross_margin")
        assert rows[0][0] == 2
    finally:
        wh2.close()
