"""Tests for the `bpd_health_check` tool (Patch #3).

Each of the 15 checks gets a pass-path test and (where the check has a non-trivial
failure mode) a fail-path test driven by deliberately broken state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from pydantic import SecretStr

from bpd_mcp.auth import AuthManager, TokenBundle
from bpd_mcp.client import KiteworksClient, make_http_client
from bpd_mcp.config import Settings
from bpd_mcp.schemas import HealthCheckInput
from bpd_mcp.tools.admin import (
    EXPECTED_LEDGER_COLUMNS,
    EXPECTED_TOOL_COUNT,
    health_check,
)
from bpd_mcp.warehouse import Warehouse


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="aubrey@biom.com",
        kiteworks_password=SecretStr("p"),
        kiteworks_client_id="cid",
        kiteworks_client_secret=SecretStr("csec"),
        bpd_data_dir=str(tmp_path),
        bpd_vendor_id="139440",
    )


def _fresh_bundle() -> TokenBundle:
    # Beyond the 24h warning window so token_expiry_window comes back "pass".
    return TokenBundle(
        access_token="AT-fresh",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )


# --------- helper: wire AppContext-like deps for the health_check coro ---------


_UNSET = object()  # sentinel so we can distinguish "no bundle" from "default"


async def _run_checks(
    *,
    tmp_path: Path,
    warehouse: Warehouse | None = None,
    bundle: TokenBundle | None | object = _UNSET,
    skip_network: bool = True,
    settings: Settings | None = None,
):
    if bundle is _UNSET:
        bundle = _fresh_bundle()
    s = settings or _settings(tmp_path)
    s.ensure_dirs()
    if bundle is not None:
        # Write the bundle so auth_token_valid passes its file-mode check too.
        from bpd_mcp.auth import _atomic_write

        _atomic_write(s.token_file, bundle.to_json())
    http = make_http_client(s)
    auth = AuthManager(s, http, bundle=bundle)
    client = KiteworksClient(s, auth, http)
    wh = warehouse or Warehouse(s.db_path)
    try:
        resp = await health_check(
            auth=auth,
            client=client,
            warehouse=wh,
            settings=s,
            params=HealthCheckInput(skip_network=skip_network, response_format="json"),
        )
    finally:
        await http.aclose()
        if warehouse is None:
            wh.close()
    return resp


def _by_name(resp) -> dict:
    return {r["name"]: r for r in resp.data["rows"]}


# --------- positive path: every check passes on a healthy install ---------


async def test_health_check_all_pass_on_healthy_install(tmp_path: Path) -> None:
    resp = await _run_checks(tmp_path=tmp_path)
    assert resp.ok is True
    by = _by_name(resp)
    # All 15 checks present.
    expected_names = {
        "auth_token_valid",
        "auth_kiteworks_reachable",
        "warehouse_file_exists",
        "warehouse_no_legacy_snapshot",
        "warehouse_schema_current",
        "warehouse_ro_enforced",
        "warehouse_rw_writable",
        "sync_ledger_consistent",
        "sync_no_orphan_raw_files",
        "datasets_have_data",
        "warehouse_no_duplicate_rows",
        "disk_usage",
        "token_expiry_window",
        "config_validity",
        "mcp_self_check",
    }
    assert set(by) == expected_names
    # On a fresh warehouse `datasets_have_data` warns (no data yet).
    assert by["warehouse_file_exists"]["status"] == "pass"
    assert by["warehouse_no_legacy_snapshot"]["status"] == "pass"
    assert by["warehouse_schema_current"]["status"] == "pass"
    assert by["warehouse_ro_enforced"]["status"] == "pass"
    assert by["warehouse_rw_writable"]["status"] == "pass"
    assert by["sync_ledger_consistent"]["status"] == "pass"
    assert by["sync_no_orphan_raw_files"]["status"] == "pass"
    assert by["auth_token_valid"]["status"] == "pass"
    assert by["auth_kiteworks_reachable"]["status"] == "warn"  # skip_network=True
    assert by["token_expiry_window"]["status"] == "pass"
    assert by["config_validity"]["status"] == "pass"
    assert by["mcp_self_check"]["status"] == "pass"
    assert by["disk_usage"]["status"] == "pass"
    assert by["warehouse_no_duplicate_rows"]["status"] == "pass"


async def test_health_check_warehouse_no_duplicate_rows_warns_on_dup(
    tmp_path: Path,
) -> None:
    """Patch #6.1 follow-up. If a data table has literal-row duplicates (every
    column value identical), the check must warn with the dataset name and
    duplicate count so the user can remediate via `bpd_refresh_dataset(full=True)`.
    """
    from datetime import date

    import polars as pl

    from bpd_mcp.parsers import derive_duckdb_schema

    s = _settings(tmp_path)
    s.ensure_dirs()
    wh = Warehouse(s.db_path)
    df = pl.DataFrame(
        {
            "tcin": [100, 100, 200],  # row 1 == row 0 by every column → dup
            "location_id": [1234, 1234, 5678],
            "week_end_date": [date(2026, 5, 9), date(2026, 5, 9), date(2026, 5, 9)],
            "units_sold": [10, 10, 20],
        }
    )
    cols = derive_duckdb_schema(df)
    wh.register_schema("sales_weekly", cols, ("tcin", "location_id", "week_end_date"))
    wh.ensure_data_table("sales_weekly", cols)
    # Bypass upsert (which would dedup by PK) — insert directly to plant a dup.
    wh._conn.register("incoming_df", df.to_arrow())  # type: ignore[attr-defined]
    wh._conn.execute("INSERT INTO sales_weekly SELECT * FROM incoming_df")  # type: ignore[attr-defined]
    wh._conn.unregister("incoming_df")  # type: ignore[attr-defined]

    resp = await _run_checks(tmp_path=tmp_path, warehouse=wh)
    wh.close()
    by = _by_name(resp)
    check = by["warehouse_no_duplicate_rows"]
    assert check["status"] == "warn"
    assert "sales_weekly" in check["detail"]
    assert "1 of 3" in check["detail"]
    assert "bpd_refresh_dataset" in check["detail"]


# --------- individual failure modes ---------


async def test_health_check_warehouse_no_legacy_snapshot_fails(tmp_path: Path) -> None:
    """When a `.ro` file is present, this check must fail loudly."""
    s = _settings(tmp_path)
    s.ensure_dirs()
    # Plant a stale .ro file.
    legacy = s.db_path.with_suffix(s.db_path.suffix + ".ro")
    legacy.write_bytes(b"stale")
    resp = await _run_checks(tmp_path=tmp_path, settings=s)
    by = _by_name(resp)
    assert by["warehouse_no_legacy_snapshot"]["status"] == "fail"
    assert ".ro" in by["warehouse_no_legacy_snapshot"]["detail"]
    assert resp.data["overall_status"] == "fail"


async def test_health_check_warehouse_schema_current_fails_on_missing_column(
    tmp_path: Path,
) -> None:
    """If `_file_ledger` is missing an expected column, this check fails."""
    s = _settings(tmp_path)
    s.ensure_dirs()
    # Build the file manually with an old schema (no error_message / parse_method).
    import duckdb

    conn = duckdb.connect(str(s.db_path))
    conn.execute(
        """
        CREATE TABLE _file_ledger (
            file_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            folder_id TEXT,
            dataset TEXT NOT NULL,
            file_date DATE,
            bytes BIGINT,
            fingerprint TEXT,
            downloaded_at TIMESTAMP NOT NULL,
            loaded_at TIMESTAMP,
            row_count BIGINT,
            status TEXT NOT NULL
        )
        """
    )
    conn.close()
    # Open with our Warehouse: migrations should run and add the columns. So this
    # check normally PASSES. To force the failure, drop one of the added columns
    # back out before running the health check.
    wh = Warehouse(s.db_path)
    wh.execute_sql("ALTER TABLE _file_ledger DROP COLUMN error_message")
    try:
        resp = await _run_checks(tmp_path=tmp_path, warehouse=wh, settings=s)
    finally:
        wh.close()
    by = _by_name(resp)
    assert by["warehouse_schema_current"]["status"] == "fail"
    assert "error_message" in by["warehouse_schema_current"]["detail"]


async def test_health_check_warehouse_ro_enforced_passes(tmp_path: Path) -> None:
    """The RO enforcement check should pass on the new design."""
    resp = await _run_checks(tmp_path=tmp_path)
    by = _by_name(resp)
    assert by["warehouse_ro_enforced"]["status"] == "pass"
    assert "engine rejects" in by["warehouse_ro_enforced"]["detail"].lower()


async def test_health_check_sync_ledger_consistent_warns_on_invariant_break(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()
    wh = Warehouse(s.db_path)
    # Plant a bad row: status='loaded' AND row_count IS NULL.
    wh.ledger_upsert(
        {
            "file_id": "bad-1",
            "file_name": "x.zip",
            "folder_id": "F",
            "dataset": "sales_daily",
            "file_date": None,
            "bytes": 1,
            "fingerprint": "x",
            "downloaded_at": datetime.now(UTC),
            "loaded_at": datetime.now(UTC),
            "row_count": None,  # invariant violation
            "status": "loaded",
            "error_message": None,
            "parse_method": "strict",
        }
    )
    try:
        resp = await _run_checks(tmp_path=tmp_path, warehouse=wh, settings=s)
    finally:
        wh.close()
    by = _by_name(resp)
    assert by["sync_ledger_consistent"]["status"] == "warn"
    assert "row_count IS NULL" in by["sync_ledger_consistent"]["detail"]


async def test_health_check_sync_orphan_files_warns(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ensure_dirs()
    # Plant a zip on disk that isn't in the ledger.
    (s.raw_dir / "orphan.zip").write_bytes(b"PK\x03\x04")
    resp = await _run_checks(tmp_path=tmp_path, settings=s)
    by = _by_name(resp)
    assert by["sync_no_orphan_raw_files"]["status"] == "warn"
    assert "orphan.zip" in by["sync_no_orphan_raw_files"]["detail"]


async def test_health_check_config_validity_fails_on_missing_username(
    tmp_path: Path,
) -> None:
    s = Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username=None,  # missing
        kiteworks_password=SecretStr("p"),
        bpd_data_dir=str(tmp_path),
    )
    resp = await _run_checks(tmp_path=tmp_path, settings=s)
    by = _by_name(resp)
    assert by["config_validity"]["status"] == "fail"
    assert "KITEWORKS_USERNAME" in by["config_validity"]["detail"]


async def test_health_check_token_expiry_warns_when_close(tmp_path: Path) -> None:
    near = TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) + timedelta(hours=1),  # < 24h window
    )
    resp = await _run_checks(tmp_path=tmp_path, bundle=near)
    by = _by_name(resp)
    assert by["token_expiry_window"]["status"] == "warn"
    assert "expires in" in by["token_expiry_window"]["detail"]


async def test_health_check_token_expiry_warns_when_expired(tmp_path: Path) -> None:
    expired = TokenBundle(
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    resp = await _run_checks(tmp_path=tmp_path, bundle=expired)
    by = _by_name(resp)
    assert by["token_expiry_window"]["status"] == "warn"
    # The auth_token_valid check also warns separately.
    assert by["auth_token_valid"]["status"] == "warn"


async def test_health_check_auth_token_missing(tmp_path: Path) -> None:
    """No token bundle, no on-disk file."""
    resp = await _run_checks(tmp_path=tmp_path, bundle=None)
    by = _by_name(resp)
    assert by["auth_token_valid"]["status"] == "warn"
    assert "bpd-bootstrap" in by["auth_token_valid"]["detail"]


@respx.mock
async def test_health_check_kiteworks_reachable_passes_on_200(tmp_path: Path) -> None:
    respx.get("https://securesharek.target.com/rest/users/me").mock(
        return_value=httpx.Response(
            200, json={"email": "aubrey@biom.com", "name": "Aubrey"}
        )
    )
    resp = await _run_checks(tmp_path=tmp_path, skip_network=False)
    by = _by_name(resp)
    assert by["auth_kiteworks_reachable"]["status"] == "pass"
    assert "aubrey@biom.com" in by["auth_kiteworks_reachable"]["detail"]


@respx.mock
async def test_health_check_kiteworks_reachable_fails_on_401(tmp_path: Path) -> None:
    respx.get("https://securesharek.target.com/rest/users/me").mock(
        return_value=httpx.Response(401, json={"error": "expired"})
    )
    respx.post("https://securesharek.target.com/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    resp = await _run_checks(tmp_path=tmp_path, skip_network=False, bundle=None)
    by = _by_name(resp)
    assert by["auth_kiteworks_reachable"]["status"] == "fail"


async def test_health_check_mcp_self_check_passes(tmp_path: Path) -> None:
    resp = await _run_checks(tmp_path=tmp_path)
    by = _by_name(resp)
    assert by["mcp_self_check"]["status"] == "pass"
    assert str(EXPECTED_TOOL_COUNT) in by["mcp_self_check"]["detail"]


async def test_health_check_overall_status_aggregation(tmp_path: Path) -> None:
    """Overall = fail if any fail; warn if any warn and no fail; pass otherwise."""
    # Healthy install: warn (no data yet + skip_network) → overall=warn.
    resp = await _run_checks(tmp_path=tmp_path)
    assert resp.data["overall_status"] == "warn"
    # Plant a .ro file → overall=fail.
    s = _settings(tmp_path)
    s.ensure_dirs()
    s.db_path.with_suffix(s.db_path.suffix + ".ro").write_bytes(b"x")
    resp = await _run_checks(tmp_path=tmp_path, settings=s)
    assert resp.data["overall_status"] == "fail"


async def test_health_check_records_duration_ms(tmp_path: Path) -> None:
    resp = await _run_checks(tmp_path=tmp_path)
    for r in resp.data["rows"]:
        assert isinstance(r["duration_ms"], int)
        assert r["duration_ms"] >= 0


def test_expected_ledger_columns_matches_constant() -> None:
    """The EXPECTED_LEDGER_COLUMNS tuple should be the full 13-column list."""
    assert len(EXPECTED_LEDGER_COLUMNS) == 13
