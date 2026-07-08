"""Admin tools: auth_status, cache_status, clear_cache, health_check."""

from __future__ import annotations

import shutil
import stat
import sys
import time
from datetime import UTC, datetime
from typing import Any

import duckdb

from ..auth import AuthManager
from ..client import KiteworksAPIError, KiteworksClient
from ..config import Settings
from ..formatting import (
    make_error_response,
    make_kv_response,
    make_table_response,
)
from ..parsers import PATTERNS
from ..schemas import (
    AuthStatusInput,
    CacheStatusInput,
    ClearCacheInput,
    HealthCheckInput,
    HealthCheckResult,
    ToolResponse,
)
from ..warehouse import Warehouse, quote_ident


async def auth_status(
    auth: AuthManager, client: KiteworksClient, params: AuthStatusInput
) -> ToolResponse:
    data: dict[str, Any] = dict(auth.status())
    # Best-effort whoami for the email.
    if data.get("authenticated"):
        try:
            me = await client.whoami()
            data["user_email"] = me.get("email") or me.get("userPrincipalName")
            data["user_name"] = me.get("name")
        except KiteworksAPIError as e:
            data["whoami_error"] = f"HTTP {e.status}: {e}"
        except Exception as e:
            data["whoami_error"] = str(e)
    return make_kv_response(data=data, title="Auth status", fmt=params.response_format)


def _dir_bytes(path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


async def cache_status(
    warehouse: Warehouse, settings: Settings, params: CacheStatusInput
) -> ToolResponse:
    from ..column_roles import DATASET_KINDS

    info = warehouse.disk_stats()
    raw_bytes = _dir_bytes(settings.raw_dir)
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    dataset_rows = warehouse.list_datasets()

    # Per-dataset breakdown: which date column was detected, the row count, and
    # min/max. When a dataset has no date column, we expose it as null so the
    # caller can tell "we tried" from "no data at all".
    per_dataset: list[dict[str, Any]] = []
    for r in dataset_rows:
        date_col = warehouse.detect_date_column(r["dataset"])
        per_dataset.append(
            {
                "dataset": r["dataset"],
                "kind": DATASET_KINDS.get(r["dataset"], "unknown"),
                "row_count": r["row_count"],
                "date_column": date_col,
                "min_date": r["min_date"],
                "max_date": r["max_date"],
                "file_count": r["file_count"],
                "last_loaded_at": r["last_loaded_at"],
            }
        )

    # Split into transactional vs dimensional. The transactional range is the
    # business-meaningful "what data do we have"; the all-datasets range
    # additionally includes dimensional tables like location_attr whose date
    # extent (e.g. last_remodel_date back to 2000) isn't relevant for business
    # data freshness.
    def _bounds(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
        mn = min((r["min_date"] for r in rows if r["min_date"] is not None), default=None)
        mx = max((r["max_date"] for r in rows if r["max_date"] is not None), default=None)
        return mn, mx

    transactional_rows = [
        r for r in per_dataset if r["kind"] == "transactional"
    ]
    tx_min, tx_max = _bounds(transactional_rows)
    all_min, all_max = _bounds(per_dataset)

    payload = {
        "data_dir": str(settings.data_dir),
        "raw_dir_bytes": raw_bytes,
        "duckdb_file_bytes": db_bytes,
        "ledger_files": info["ledger_file_count"],
        "ledger_total_bytes": info["ledger_bytes_total"],
        "datasets_loaded": len(dataset_rows),
        # Business-data range (transactional datasets only).
        "earliest_data_date": tx_min,
        "latest_data_date": tx_max,
        # All-datasets range (includes dimensional tables).
        "earliest_data_date_including_dimensional": all_min,
        "latest_data_date_including_dimensional": all_max,
        "last_sync_finished_at": info["last_sync_finished_at"],
        "per_dataset": per_dataset,
    }
    return make_kv_response(data=payload, title="Cache status", fmt=params.response_format)


async def clear_cache(
    warehouse: Warehouse, settings: Settings, params: ClearCacheInput
) -> ToolResponse:
    raw_bytes = _dir_bytes(settings.raw_dir)
    extract_bytes = _dir_bytes(settings.extract_dir)
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    preview = {
        "would_delete_raw_dir": str(settings.raw_dir),
        "would_delete_raw_bytes": raw_bytes,
        "would_delete_extract_dir": str(settings.extract_dir),
        "would_delete_extract_bytes": extract_bytes,
        "would_delete_db": str(settings.db_path),
        "would_delete_db_bytes": db_bytes,
        "confirm_supplied": params.confirm,
    }
    if not params.confirm:
        preview["dry_run"] = True
        return make_kv_response(
            data=preview,
            title="Cache clear preview (no `confirm=true`)",
            fmt=params.response_format,
        )

    # Destructive path.
    try:
        if settings.raw_dir.exists():
            shutil.rmtree(settings.raw_dir, ignore_errors=True)
        if settings.extract_dir.exists():
            shutil.rmtree(settings.extract_dir, ignore_errors=True)
        # Close the warehouse before deleting its file.
        warehouse.close()
        if settings.db_path.exists():
            settings.db_path.unlink()
        wal = settings.db_path.with_suffix(settings.db_path.suffix + ".wal")
        if wal.exists():
            wal.unlink()
        settings.ensure_dirs()
        preview["dry_run"] = False
        preview["status"] = "cleared"
        return make_kv_response(
            data=preview, title="Cache cleared", fmt=params.response_format
        )
    except Exception as e:
        return make_error_response(
            code="CACHE_CLEAR_FAILED", message=str(e), fmt=params.response_format
        )


# --------------------------------------------------------------------------------------
# bpd_health_check (Patch #3)
# --------------------------------------------------------------------------------------


# Expected count of registered MCP tools after this patch lands.
# Lineage: 16 base + 3 S&OP analytics (patch #2) + bpd_health_check (patch #3) +
# bpd_export_query_to_csv (patch #4) = 21.
EXPECTED_TOOL_COUNT = 21

# Columns the patched ledger must have. Used by `warehouse_schema_current`.
EXPECTED_LEDGER_COLUMNS = (
    "file_id",
    "file_name",
    "folder_id",
    "dataset",
    "file_date",
    "bytes",
    "fingerprint",
    "downloaded_at",
    "loaded_at",
    "row_count",
    "status",
    "error_message",
    "parse_method",
)


def _timed(fn):
    """Decorator: wrap a check coroutine so it records duration_ms automatically."""

    async def wrapper(*args, **kwargs) -> HealthCheckResult:
        t0 = time.perf_counter()
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:  # check itself crashed — that's a hard fail
            result = HealthCheckResult(
                name=fn.__name__.lstrip("_"),
                status="fail",
                detail=f"check raised: {type(e).__name__}: {e}",
            )
        result_dict = result.model_dump()
        result_dict["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        return HealthCheckResult(**result_dict)

    return wrapper


# ---------- individual checks ----------


@_timed
async def _auth_token_valid(
    auth: AuthManager, settings: Settings, **_: Any
) -> HealthCheckResult:
    path = settings.token_file
    if not path.exists():
        return HealthCheckResult(
            name="auth_token_valid",
            status="warn",
            detail=f"token file {path} does not exist — run `bpd-bootstrap`",
        )
    if not sys.platform.startswith("win"):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return HealthCheckResult(
                name="auth_token_valid",
                status="fail",
                detail=f"token file mode {oct(mode)} is insecure; run `chmod 600 {path}`",
            )
    bundle = auth.bundle
    if bundle is None:
        return HealthCheckResult(
            name="auth_token_valid",
            status="warn",
            detail="token file present but bundle not loaded; try restarting the MCP",
        )
    if bundle.is_expired(skew_seconds=0):
        return HealthCheckResult(
            name="auth_token_valid",
            status="warn",
            detail=f"access token expired at {bundle.expires_at.isoformat()}; "
            "refresh will run on next API call",
        )
    return HealthCheckResult(
        name="auth_token_valid",
        status="pass",
        detail=f"token valid until {bundle.expires_at.isoformat()}",
    )


@_timed
async def _auth_kiteworks_reachable(
    client: KiteworksClient, skip_network: bool, **_: Any
) -> HealthCheckResult:
    if skip_network:
        return HealthCheckResult(
            name="auth_kiteworks_reachable",
            status="warn",
            detail="skipped (skip_network=True)",
        )
    try:
        me = await client.whoami()
    except KiteworksAPIError as e:
        return HealthCheckResult(
            name="auth_kiteworks_reachable",
            status="fail",
            detail=f"HTTP {e.status}: {e.body or e}",
        )
    except Exception as e:
        return HealthCheckResult(
            name="auth_kiteworks_reachable",
            status="fail",
            detail=f"{type(e).__name__}: {e}",
        )
    email = me.get("email") or me.get("userPrincipalName") or "(unknown)"
    return HealthCheckResult(
        name="auth_kiteworks_reachable",
        status="pass",
        detail=f"GET /rest/users/me 200 OK; user={email}",
    )


@_timed
async def _warehouse_file_exists(
    warehouse: Warehouse, settings: Settings, **_: Any
) -> HealthCheckResult:
    if not settings.db_path.exists():
        return HealthCheckResult(
            name="warehouse_file_exists",
            status="warn",
            detail=f"{settings.db_path} not yet created — run `bpd_sync_new_files`",
        )
    # The fact that we have a live `warehouse` object IS the proof the file is
    # openable; a probe SELECT via the live connection is enough to confirm.
    try:
        warehouse.execute_sql("SELECT 1")
    except Exception as e:
        return HealthCheckResult(
            name="warehouse_file_exists",
            status="fail",
            detail=f"file present but live connection failed: {type(e).__name__}: {e}",
        )
    return HealthCheckResult(
        name="warehouse_file_exists",
        status="pass",
        detail=f"{settings.db_path} ({settings.db_path.stat().st_size} bytes)",
    )


@_timed
async def _warehouse_no_legacy_snapshot(
    settings: Settings, **_: Any
) -> HealthCheckResult:
    p = settings.db_path
    legacy = [p.with_suffix(p.suffix + ".ro"), p.with_suffix(p.suffix + ".ro.wal")]
    extant = [str(x) for x in legacy if x.exists()]
    if extant:
        return HealthCheckResult(
            name="warehouse_no_legacy_snapshot",
            status="fail",
            detail=(
                f"legacy snapshot file(s) present: {extant}. The .ro snapshot design "
                "was removed in patch #3. Stop the MCP, delete these files, restart."
            ),
        )
    return HealthCheckResult(
        name="warehouse_no_legacy_snapshot",
        status="pass",
        detail="no legacy .ro / .ro.wal files present",
    )


@_timed
async def _warehouse_schema_current(
    warehouse: Warehouse, **_: Any
) -> HealthCheckResult:
    _, rows = warehouse.execute_sql("PRAGMA table_info('_file_ledger')")
    actual = {r[1] for r in rows}
    missing = [c for c in EXPECTED_LEDGER_COLUMNS if c not in actual]
    if missing:
        return HealthCheckResult(
            name="warehouse_schema_current",
            status="fail",
            detail=f"_file_ledger missing columns: {missing}. Run `bpd_sync_new_files` to apply migrations.",
        )
    # All metadata tables should exist.
    _, t_rows = warehouse.execute_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE'"
    )
    tables = {r[0] for r in t_rows}
    required = {"_file_ledger", "_sync_log", "_schema_registry"}
    missing_meta = required - tables
    if missing_meta:
        return HealthCheckResult(
            name="warehouse_schema_current",
            status="fail",
            detail=f"missing metadata tables: {sorted(missing_meta)}",
        )
    return HealthCheckResult(
        name="warehouse_schema_current",
        status="pass",
        detail=f"_file_ledger has {len(actual)} cols; all metadata tables present",
    )


@_timed
async def _warehouse_ro_enforced(warehouse: Warehouse, **_: Any) -> HealthCheckResult:
    """Verify the ReadOnlyView rejects writes at the engine layer."""
    from ..warehouse import ReadOnlyView

    ro = ReadOnlyView(warehouse)
    try:
        ro.execute_sql("CREATE TABLE _health_check_ro_probe (a INT)")
    except duckdb.Error:
        # Expected: DuckDB refused the write inside the read-only transaction.
        # Confirm the table wasn't actually created.
        _, rows = warehouse.execute_sql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = '_health_check_ro_probe'"
        )
        if rows[0][0] == 0:
            return HealthCheckResult(
                name="warehouse_ro_enforced",
                status="pass",
                detail="engine rejects writes inside BEGIN TRANSACTION READ ONLY",
            )
    # We got here either because the CREATE succeeded (very bad) or DuckDB raised
    # but the table somehow exists.
    return HealthCheckResult(
        name="warehouse_ro_enforced",
        status="fail",
        detail="SECURITY: read-only enforcement is not active — CREATE TABLE via the read-only view succeeded",
    )


@_timed
async def _warehouse_rw_writable(warehouse: Warehouse, **_: Any) -> HealthCheckResult:
    """Write+read+drop a temp table to confirm the rw connection is functional."""
    try:
        warehouse.execute_sql("CREATE TEMPORARY TABLE _hc_rw_probe (x INT)")
        warehouse.execute_sql("INSERT INTO _hc_rw_probe VALUES (1), (2)")
        _, rows = warehouse.execute_sql("SELECT SUM(x) FROM _hc_rw_probe")
        warehouse.execute_sql("DROP TABLE _hc_rw_probe")
    except Exception as e:
        return HealthCheckResult(
            name="warehouse_rw_writable",
            status="fail",
            detail=f"write probe failed: {type(e).__name__}: {e}",
        )
    if rows[0][0] != 3:
        return HealthCheckResult(
            name="warehouse_rw_writable",
            status="fail",
            detail=f"write probe returned unexpected value: {rows[0][0]} (expected 3)",
        )
    return HealthCheckResult(
        name="warehouse_rw_writable",
        status="pass",
        detail="temp-table write/read/drop round-trip succeeded",
    )


@_timed
async def _sync_ledger_consistent(warehouse: Warehouse, **_: Any) -> HealthCheckResult:
    issues: list[str] = []

    _, r1 = warehouse.execute_sql(
        "SELECT COUNT(*) FROM _file_ledger WHERE status='loaded' AND row_count IS NULL"
    )
    if r1[0][0] > 0:
        issues.append(f"{r1[0][0]} rows with status='loaded' but row_count IS NULL")

    _, r2 = warehouse.execute_sql(
        "SELECT COUNT(*) FROM _file_ledger WHERE downloaded_at IS NULL AND status != 'queued'"
    )
    if r2[0][0] > 0:
        issues.append(
            f"{r2[0][0]} rows with downloaded_at IS NULL and status != 'queued'"
        )

    _, r3 = warehouse.execute_sql(
        "SELECT COUNT(*) FROM _file_ledger WHERE loaded_at IS NOT NULL AND status != 'loaded'"
    )
    if r3[0][0] > 0:
        issues.append(
            f"{r3[0][0]} rows with loaded_at set but status != 'loaded'"
        )

    if issues:
        return HealthCheckResult(
            name="sync_ledger_consistent",
            status="warn",
            detail="; ".join(issues),
        )
    return HealthCheckResult(
        name="sync_ledger_consistent",
        status="pass",
        detail="ledger invariants hold",
    )


@_timed
async def _sync_no_orphan_raw_files(
    warehouse: Warehouse, settings: Settings, **_: Any
) -> HealthCheckResult:
    """Raw zips on disk that aren't in the ledger.

    Note (audit): mild drift here is acceptable — sync is idempotent, and the LRU
    cap may evict zips whose ledger entries still exist. We report orphans as a
    warning, not a failure.
    """
    if not settings.raw_dir.exists():
        return HealthCheckResult(
            name="sync_no_orphan_raw_files",
            status="pass",
            detail=f"{settings.raw_dir} does not exist (no sync yet)",
        )
    on_disk = {p.name for p in settings.raw_dir.glob("*.zip") if p.is_file()}
    _, rows = warehouse.execute_sql("SELECT file_name FROM _file_ledger")
    in_ledger = {r[0] for r in rows}
    orphans = sorted(on_disk - in_ledger)
    if orphans:
        sample = ", ".join(orphans[:3])
        more = f" (+{len(orphans) - 3} more)" if len(orphans) > 3 else ""
        return HealthCheckResult(
            name="sync_no_orphan_raw_files",
            status="warn",
            detail=f"{len(orphans)} zip(s) on disk but not in ledger: {sample}{more}",
        )
    return HealthCheckResult(
        name="sync_no_orphan_raw_files",
        status="pass",
        detail=f"all {len(on_disk)} raw zip(s) are in the ledger",
    )


@_timed
async def _datasets_have_data(warehouse: Warehouse, **_: Any) -> HealthCheckResult:
    info = warehouse.describe()["tables"]
    populated = 0
    empty = []
    # Dedupe: multiple patterns may map to one dataset (HISTORY backfill).
    for ds in dict.fromkeys(p.dataset for p in PATTERNS):
        if ds not in info:
            continue
        if info[ds]["row_count"] > 0:
            populated += 1
        else:
            empty.append(ds)
    total_known = sum(1 for d in dict.fromkeys(p.dataset for p in PATTERNS) if d in info)
    if total_known == 0:
        return HealthCheckResult(
            name="datasets_have_data",
            status="warn",
            detail="no dataset tables present — run bpd_sync_new_files",
        )
    if not empty:
        return HealthCheckResult(
            name="datasets_have_data",
            status="pass",
            detail=f"{populated}/{total_known} dataset(s) populated",
        )
    return HealthCheckResult(
        name="datasets_have_data",
        status="warn",
        detail=(
            f"{populated}/{total_known} dataset(s) populated; "
            f"empty: {empty} (likely the user is not subscribed to these)"
        ),
    )


@_timed
async def _warehouse_no_duplicate_rows(
    warehouse: Warehouse, **_: Any
) -> HealthCheckResult:
    """Detect literal-row duplicates across data tables.

    A healthy warehouse has zero rows where every column value is bitwise
    identical to another row. Our `upsert_dataframe` is delete-then-insert
    keyed on the dataset's primary_key, so re-loading a file should always
    leave row counts unchanged. If this check warns, the data has either:
    (a) been loaded by a path that bypassed upsert, (b) been duplicated by
    a `bpd_refresh_dataset(full=False)` against a table whose primary_key
    columns don't actually appear in the df (the warehouse logs
    `primary_key_missing_in_df` in that case), or (c) been corrupted by an
    external write. A full refresh (`bpd_refresh_dataset(<ds>, full=True)`)
    is the standard remediation.
    """
    info = warehouse.describe()["tables"]
    dup_tables: list[tuple[str, int, int]] = []
    # Dedupe: multiple patterns may map to one dataset (HISTORY backfill).
    for ds in dict.fromkeys(p.dataset for p in PATTERNS):
        if ds not in info or info[ds]["row_count"] == 0:
            continue
        tbl = quote_ident(ds)
        _, r = warehouse.execute_sql(
            f"SELECT COUNT(*), "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {tbl})) "
            f"FROM {tbl}"
        )
        total, distinct_count = r[0]
        if total > distinct_count:
            dup_tables.append((ds, total, total - distinct_count))
    if not dup_tables:
        return HealthCheckResult(
            name="warehouse_no_duplicate_rows",
            status="pass",
            detail="no full-row duplicates across data tables",
        )
    parts = [f"{ds}: {dup} of {total}" for ds, total, dup in dup_tables]
    return HealthCheckResult(
        name="warehouse_no_duplicate_rows",
        status="warn",
        detail=(
            "literal-row duplicates detected — "
            + "; ".join(parts)
            + " (remediation: bpd_refresh_dataset(<dataset>, full=True))"
        ),
    )


@_timed
async def _disk_usage(settings: Settings, **_: Any) -> HealthCheckResult:
    if not settings.data_dir.exists():
        return HealthCheckResult(
            name="disk_usage",
            status="pass",
            detail=f"{settings.data_dir} does not exist yet",
        )
    total = 0
    for p in settings.data_dir.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    gib = total / (1024**3)
    cap_gib = settings.bpd_raw_dir_max_bytes / (1024**3)
    if total > 4 * (1024**3):
        return HealthCheckResult(
            name="disk_usage",
            status="warn",
            detail=f"data_dir = {gib:.2f} GiB (cap is {cap_gib:.2f} GiB)",
        )
    return HealthCheckResult(
        name="disk_usage",
        status="pass",
        detail=f"data_dir = {gib:.2f} GiB",
    )


@_timed
async def _token_expiry_window(auth: AuthManager, **_: Any) -> HealthCheckResult:
    bundle = auth.bundle
    if bundle is None:
        return HealthCheckResult(
            name="token_expiry_window",
            status="warn",
            detail="no token bundle loaded",
        )
    secs = (bundle.expires_at - datetime.now(UTC)).total_seconds()
    if secs < 0:
        return HealthCheckResult(
            name="token_expiry_window",
            status="warn",
            detail=f"access token expired {-int(secs)}s ago; refresh runs on next call",
        )
    if secs < 24 * 3600:
        return HealthCheckResult(
            name="token_expiry_window",
            status="warn",
            detail=f"access token expires in {int(secs / 3600)}h",
        )
    return HealthCheckResult(
        name="token_expiry_window",
        status="pass",
        detail=f"access token expires in {secs / 3600:.1f}h",
    )


@_timed
async def _config_validity(settings: Settings, **_: Any) -> HealthCheckResult:
    issues: list[str] = []
    if not settings.kiteworks_username:
        issues.append("KITEWORKS_USERNAME is not set")
    if not settings.bpd_vendor_id or not str(settings.bpd_vendor_id).strip():
        issues.append("BPD_VENDOR_ID is not set")
    if settings.bpd_vendor_tier not in ("BV", "BR", "CC"):
        issues.append(f"BPD_VENDOR_TIER={settings.bpd_vendor_tier!r} is not BV/BR/CC")
    if not settings.kiteworks_base_url.startswith("https://"):
        issues.append(
            f"KITEWORKS_BASE_URL={settings.kiteworks_base_url!r} is not https"
        )
    if issues:
        return HealthCheckResult(
            name="config_validity",
            status="fail",
            detail="; ".join(issues),
        )
    return HealthCheckResult(
        name="config_validity",
        status="pass",
        detail=(
            f"username={settings.kiteworks_username}, "
            f"vendor={settings.bpd_vendor_id}, tier={settings.bpd_vendor_tier}"
        ),
    )


@_timed
async def _mcp_self_check(**_: Any) -> HealthCheckResult:
    from ..server import mcp

    tools = sorted(mcp._tool_manager._tools.keys())
    if len(tools) < EXPECTED_TOOL_COUNT:
        return HealthCheckResult(
            name="mcp_self_check",
            status="fail",
            detail=(
                f"only {len(tools)}/{EXPECTED_TOOL_COUNT} tools registered. "
                f"Tools: {tools}"
            ),
        )
    if len(tools) > EXPECTED_TOOL_COUNT:
        return HealthCheckResult(
            name="mcp_self_check",
            status="warn",
            detail=(
                f"{len(tools)} tools registered (expected {EXPECTED_TOOL_COUNT}); "
                "did someone add a tool without bumping the expected count?"
            ),
        )
    return HealthCheckResult(
        name="mcp_self_check",
        status="pass",
        detail=f"all {len(tools)} expected tools registered",
    )


async def health_check(
    *,
    auth: AuthManager,
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    params: HealthCheckInput,
) -> ToolResponse:
    """Run all health checks and return a structured report."""
    common = {
        "auth": auth,
        "client": client,
        "warehouse": warehouse,
        "settings": settings,
        "skip_network": params.skip_network,
    }
    checks: list[HealthCheckResult] = []
    # Order matters: cheaper / no-network checks first so a failure surfaces fast.
    checks.append(await _auth_token_valid(**common))
    checks.append(await _warehouse_file_exists(**common))
    checks.append(await _warehouse_no_legacy_snapshot(**common))
    checks.append(await _warehouse_schema_current(**common))
    checks.append(await _warehouse_ro_enforced(**common))
    checks.append(await _warehouse_rw_writable(**common))
    checks.append(await _sync_ledger_consistent(**common))
    checks.append(await _sync_no_orphan_raw_files(**common))
    checks.append(await _datasets_have_data(**common))
    checks.append(await _warehouse_no_duplicate_rows(**common))
    checks.append(await _disk_usage(**common))
    checks.append(await _token_expiry_window(**common))
    checks.append(await _config_validity(**common))
    checks.append(await _mcp_self_check(**common))
    # Network last so a hang here doesn't block local diagnostics.
    checks.append(await _auth_kiteworks_reachable(**common))

    overall: str = "pass"
    if any(c.status == "fail" for c in checks):
        overall = "fail"
    elif any(c.status == "warn" for c in checks):
        overall = "warn"

    rows = [c.model_dump() for c in checks]
    payload = {
        "overall_status": overall,
        "checks": rows,
        "summary": (
            f"overall={overall}; "
            f"pass={sum(1 for c in checks if c.status == 'pass')} "
            f"warn={sum(1 for c in checks if c.status == 'warn')} "
            f"fail={sum(1 for c in checks if c.status == 'fail')}"
        ),
        "timestamp": datetime.now(UTC),
    }
    return make_table_response(
        rows=rows,
        columns=["name", "status", "detail", "duration_ms"],
        title=f"bpd_health_check — overall_status: {overall.upper()}",
        extra={
            "overall_status": payload["overall_status"],
            "summary": payload["summary"],
            "timestamp": payload["timestamp"],
        },
        fmt=params.response_format,
    )
