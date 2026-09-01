"""Admin tools: list_datasets, bigquery_status, data_freshness, health_check.

Three of these changed shape when the data layer moved from a local DuckDB file
to read-only BigQuery:

* `auth_status` -> `bigquery_status`. There is no OAuth token to report. What a
  user needs instead is *which identity am I querying as, and can it write?*
* `cache_status` -> `data_freshness`. Its disk numbers (raw-zip bytes, DuckDB
  file size, ledger totals) measured a local cache that no longer exists. Its
  freshness numbers were always the product, and they survive — now sourced
  from the upstream pipeline's own ledger, `bpd_meta.ingestion_state`.
* `clear_cache` is gone, not stubbed. A no-op "clear cache" is worse than none:
  a user who invokes it reasonably believes state was reset, then reads
  stale-looking BigQuery results as a failed reset.

`list_datasets` moved here from the deleted `tools/sync.py`, unchanged in shape.

COST DISCIPLINE. Health checks run often and BigQuery bills by byte scanned, so
every check here is either free or bounded, and each one says which:
  * `__TABLES__` row counts and dry-run schemas cost 0 bytes.
  * `bpd_meta.ingestion_state` is 834 rows.
  * The date-range sweep is one combined job (~527 MB), TTL-cached in the
    warehouse for 900 s and shared with `bpd_list_datasets`.
  * The tool smoke test DRY-RUNS by default (0 bytes); `execute=true` opts into
    a real run.
Nothing here may `COUNT(*)` through a logical table's CTE — across the roster
that is ~333 MB per call, and this module used to call `describe()` in a loop.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from ..bq import (
    BQ_INGESTION_STATE,
    BQ_META_DATASET,
    BigQueryWarehouse,
    CredentialsUnavailable,
    quote_ident,
    resolve_credentials,
)
from ..config import Settings
from ..formatting import make_kv_response, make_table_response
from ..schemas import (
    BigQueryStatusInput,
    DataFreshnessInput,
    HealthCheckInput,
    HealthCheckResult,
    ListDatasetsInput,
    ToolResponse,
)

# A feed whose newest file is older than this is treated as retired rather than
# broken. Mirrors `bq._RETIRED_AFTER_DAYS`, which drives `list_datasets.status`.
RETIRED_AFTER_DAYS = 90
# Between "current" and "retired" is the band worth warning about.
STALE_WARN_DAYS = 14

# Every logical table is expected to project these `data_grain` values and no
# others. `data_grain` is an unconstrained STRING in biom_canvas; if the
# upstream pipeline adds a fourth value, `sales_weekly`'s
# `IN ('weekly','history_weekly')` would silently ignore it.
EXPECTED_DATA_GRAINS = frozenset({"daily", "weekly", "history_weekly"})


# --------------------------------------------------------------------------------------
# bpd_list_datasets  (moved from the deleted tools/sync.py)
# --------------------------------------------------------------------------------------


async def list_datasets(
    warehouse: BigQueryWarehouse, params: ListDatasetsInput
) -> ToolResponse:
    rows = warehouse.list_datasets()
    return make_table_response(
        rows=rows,
        columns=[
            "dataset", "feed_kind", "status", "row_count",
            "min_date", "max_date", "content_max_date",
            "file_count", "last_loaded_at",
        ],
        extra={
            "notes": (
                "min/max_date = snapshot range (data freshness); "
                "content_max_date = how far the CONTENT reaches (order_d / "
                "fiscal week / ETA) — for forward-looking datasets these "
                "differ by months. feed_kind says whether to query the table "
                "whole (delta_latest_state), filter to max(business_d) "
                "(accumulating_snapshots), or neither. status=retired means "
                "Target sunset the feed. row_count is the BigQuery BASE "
                "table's row count, so it OVERSTATES any logical table that "
                "filters or de-duplicates (orders_daily and forecast_weekly "
                "most of all) — see bpd_describe_schema's latest_state_note. "
                "file_count/last_loaded_at come from the upstream pipeline's "
                "ledger and mean a FILE arrived, not that rows are queryable."
            ),
            "row_count_basis": "base_table",
        },
        title="BPD datasets (BigQuery)",
        fmt=params.response_format,
    )


# --------------------------------------------------------------------------------------
# bpd_bigquery_status  (was bpd_auth_status)
# --------------------------------------------------------------------------------------


async def bigquery_status(
    warehouse: BigQueryWarehouse, params: BigQueryStatusInput
) -> ToolResponse:
    """Who are we querying BigQuery as, and what can that identity do?

    Never returns key material: `credentials_source` is a path or the name of
    the env var the credential came from.
    """
    data: dict[str, Any] = {
        "project": warehouse.project,
        "location": warehouse.location,
        "warehouse": warehouse.db_path,
        "credentials_source": warehouse.credentials_source,
        "read_only": warehouse.read_only,
        "write_capability": "none (dataViewer + jobUser)",
        "maximum_bytes_billed": warehouse.maximum_bytes_billed,
        "logical_tables": len(warehouse.registry),
    }
    # SESSION_USER() is the authoritative answer to "which service account?" —
    # it comes back from the server, not from the local credential file.
    try:
        _, rows = warehouse.execute_sql("SELECT SESSION_USER() AS session_user")
        data["session_user"] = rows[0][0] if rows else None
    except Exception as e:
        data["session_user_error"] = f"{type(e).__name__}: {e}"
    try:
        data["datasets_reachable"] = sorted(
            d.dataset_id for d in warehouse.client.list_datasets(warehouse.project)
        )
    except Exception as e:
        data["datasets_reachable_error"] = f"{type(e).__name__}: {e}"
    return make_kv_response(
        data=data, title="BigQuery status", fmt=params.response_format
    )


# --------------------------------------------------------------------------------------
# bpd_data_freshness  (was bpd_cache_status)
# --------------------------------------------------------------------------------------


async def data_freshness(
    warehouse: BigQueryWarehouse, settings: Settings, params: DataFreshnessInput
) -> ToolResponse:
    """How current is the data, per dataset and per upstream file pattern.

    Kept from `cache_status` because they are analytical concepts, not disk
    concepts: the transactional-vs-dimensional split, and the snapshot horizon
    (`latest_data_date`, how fresh) vs the content horizon
    (`latest_content_date`, how far forward plans and forecasts reach).

    Dropped: `raw_dir_bytes`, `duckdb_file_bytes`, `ledger_files`,
    `ledger_total_bytes`, `last_sync_finished_at`. Added: the per-pattern
    pipeline block, which immediately exposes staleness the local cache hid.
    """
    from ..column_roles import DATASET_KINDS

    dataset_rows = warehouse.list_datasets()
    stats = warehouse.freshness_stats()

    per_dataset: list[dict[str, Any]] = []
    for r in dataset_rows:
        per_dataset.append(
            {
                "dataset": r["dataset"],
                "kind": DATASET_KINDS.get(r["dataset"], "unknown"),
                "feed_kind": r.get("feed_kind"),
                "status": r.get("status"),
                "row_count": r["row_count"],
                "date_column": r.get("date_column"),
                "min_date": r["min_date"],
                "max_date": r["max_date"],
                "content_column": r.get("content_column"),
                "content_min_date": r.get("content_min_date"),
                "content_max_date": r.get("content_max_date"),
                "file_count": r["file_count"],
                "last_loaded_at": r["last_loaded_at"],
            }
        )

    def _bounds(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
        mn = min((r["min_date"] for r in rows if r["min_date"] is not None), default=None)
        mx = max((r["max_date"] for r in rows if r["max_date"] is not None), default=None)
        return mn, mx

    # Split transactional from dimensional: the transactional range is the
    # business-meaningful "what data do we have". Including dimensional tables
    # drags it back to 2000 via location_attr.last_remodel_date.
    transactional_rows = [r for r in per_dataset if r["kind"] == "transactional"]
    tx_min, tx_max = _bounds(transactional_rows)
    all_min, all_max = _bounds(per_dataset)
    latest_content = max(
        (
            r["content_max_date"]
            for r in transactional_rows
            if r.get("content_max_date") is not None
        ),
        default=None,
    )

    payload = {
        "project": warehouse.project,
        "location": warehouse.location,
        "datasets": len(dataset_rows),
        # Business-data range (transactional datasets only; snapshot-based —
        # i.e. data FRESHNESS).
        "earliest_data_date": tx_min,
        "latest_data_date": tx_max,
        # Content horizon: how far forward loaded plans/forecasts/ETAs reach.
        "latest_content_date": latest_content,
        # All-datasets range (includes dimensional tables).
        "earliest_data_date_including_dimensional": all_min,
        "latest_data_date_including_dimensional": all_max,
        # Upstream pipeline ledger.
        "pipeline_last_ingest_at": stats["last_ingest_at"],
        "pipeline_total_files": stats["total_files"],
        "pipeline_patterns_seen": stats["patterns_seen"],
        "per_pattern": stats["per_pattern"],
        "per_dataset": per_dataset,
        "caveat": (
            "per_pattern.max_downloaded_at means a FILE ARRIVED in the "
            "Kiteworks -> GCS -> BigQuery pipeline (it runs daily around 06:47 "
            "UTC), not that its rows are queryable. Cross-check per_dataset."
            "max_date before telling anyone a dataset is current."
        ),
        "exports_dir": str(settings.exports_dir),
    }
    return make_kv_response(
        data=payload, title="BPD data freshness", fmt=params.response_format
    )


# --------------------------------------------------------------------------------------
# bpd_health_check
# --------------------------------------------------------------------------------------


# Registered MCP tools after the BigQuery swap. Lineage: 22 before, minus the
# four Kiteworks discovery tools, minus sync/refresh/reingest, minus
# clear_cache = 14. Kept in lockstep with server.py by `_mcp_self_check` and by
# a drift guard in the test suite — bump it in the SAME commit as any tool
# addition or removal, or every user's health check hard-fails.
EXPECTED_TOOL_COUNT = 14


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


# ---------- local checks (no BigQuery call) ----------


@_timed
async def _bq_credentials_present(
    warehouse: BigQueryWarehouse, **_: Any
) -> HealthCheckResult:
    """A usable credential resolves. Reports its SOURCE, never its contents."""
    try:
        path, source = resolve_credentials()
    except CredentialsUnavailable as e:
        return HealthCheckResult(
            name="bq_credentials_present",
            status="fail",
            detail=(
                f"{e} — set GOOGLE_APPLICATION_CREDENTIALS to a service-account "
                "JSON path, or GCP_SA_KEY_B64 to its base64 encoding."
            ),
        )
    detail = f"credential source: {source}"
    if path is not None:
        detail += f" ({path})"
    detail += f"; warehouse is using: {warehouse.credentials_source}"
    return HealthCheckResult(
        name="bq_credentials_present", status="pass", detail=detail
    )


@_timed
async def _location_configured(
    warehouse: BigQueryWarehouse, settings: Settings, **_: Any
) -> HealthCheckResult:
    """The client must be pinned to a location. FAIL, not warn.

    An unset location does not raise: it makes INFORMATION_SCHEMA silently
    return zero rows, which presents as "the table has no columns" rather than
    as an error. That is exactly the class of bug a health check exists for.
    """
    if not warehouse.location:
        return HealthCheckResult(
            name="location_configured",
            status="fail",
            detail="warehouse has no BigQuery location set (expected us-central1)",
        )
    if warehouse.location != settings.bpd_bq_location:
        return HealthCheckResult(
            name="location_configured",
            status="warn",
            detail=(
                f"warehouse location {warehouse.location!r} != configured "
                f"BPD_BQ_LOCATION {settings.bpd_bq_location!r}"
            ),
        )
    return HealthCheckResult(
        name="location_configured",
        status="pass",
        detail=f"location={warehouse.location}, project={warehouse.project}",
    )


@_timed
async def _config_validity(
    warehouse: BigQueryWarehouse, settings: Settings, **_: Any
) -> HealthCheckResult:
    issues: list[str] = []
    if not settings.bpd_bq_project.strip():
        issues.append("BPD_BQ_PROJECT is not set")
    if not settings.bpd_bq_location.strip():
        issues.append("BPD_BQ_LOCATION is not set")
    if not settings.bpd_vendor_id or not str(settings.bpd_vendor_id).strip():
        issues.append("BPD_VENDOR_ID is not set")
    if settings.bpd_vendor_tier not in ("BV", "BR", "CC"):
        issues.append(f"BPD_VENDOR_TIER={settings.bpd_vendor_tier!r} is not BV/BR/CC")
    if settings.bpd_bq_max_bytes_billed <= 0:
        issues.append("BPD_BQ_MAX_BYTES_BILLED must be positive")
    if settings.bpd_bq_warn_bytes > settings.bpd_bq_max_bytes_billed:
        issues.append(
            "BPD_BQ_WARN_BYTES is above BPD_BQ_MAX_BYTES_BILLED — the warning "
            "threshold can never fire before the hard cap rejects the job"
        )
    if issues:
        return HealthCheckResult(
            name="config_validity", status="fail", detail="; ".join(issues)
        )
    cap_gib = settings.bpd_bq_max_bytes_billed / (1024**3)
    warn_gib = settings.bpd_bq_warn_bytes / (1024**3)
    return HealthCheckResult(
        name="config_validity",
        status="pass",
        detail=(
            f"project={settings.bpd_bq_project}, location={settings.bpd_bq_location}, "
            f"vendor={settings.bpd_vendor_id}/{settings.bpd_vendor_tier}, "
            f"max_bytes_billed={cap_gib:.0f} GiB (warn at {warn_gib:.0f} GiB), "
            f"export cap={settings.bpd_export_max_rows:,} rows"
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


# ---------- BigQuery-backed checks ----------


@_timed
async def _bq_reachable_as(warehouse: BigQueryWarehouse, **_: Any) -> HealthCheckResult:
    """A real query round-trips, and we learn which identity ran it."""
    expected_project = warehouse.project
    try:
        _, rows = warehouse.execute_sql("SELECT SESSION_USER() AS session_user")
    except Exception as e:
        return HealthCheckResult(
            name="bq_reachable_as",
            status="fail",
            detail=f"cannot query BigQuery: {type(e).__name__}: {e}",
        )
    who = rows[0][0] if rows else None
    if not who:
        return HealthCheckResult(
            name="bq_reachable_as",
            status="warn",
            detail="SESSION_USER() returned nothing — identity unknown",
        )
    if not str(who).endswith(f"@{expected_project}.iam.gserviceaccount.com"):
        return HealthCheckResult(
            name="bq_reachable_as",
            status="warn",
            detail=(
                f"querying as {who}, which is not a service account of "
                f"{expected_project}. Check GOOGLE_APPLICATION_CREDENTIALS."
            ),
        )
    return HealthCheckResult(
        name="bq_reachable_as", status="pass", detail=f"querying as {who}"
    )


@_timed
async def _bq_datasets_reachable(
    warehouse: BigQueryWarehouse, **_: Any
) -> HealthCheckResult:
    """The three datasets the server reads must all be listable."""
    from ..bq import base_datasets

    required = set(base_datasets(warehouse.registry)) | {BQ_META_DATASET}
    try:
        present = {d.dataset_id for d in warehouse.client.list_datasets(warehouse.project)}
    except Exception as e:
        return HealthCheckResult(
            name="bq_datasets_reachable",
            status="fail",
            detail=f"cannot list datasets in {warehouse.project}: {type(e).__name__}: {e}",
        )
    missing = sorted(required - present)
    if missing:
        return HealthCheckResult(
            name="bq_datasets_reachable",
            status="fail",
            detail=(
                f"dataset(s) not reachable in {warehouse.project}: {missing} — "
                "the service account may have lost dataViewer, or the upstream "
                "pipeline may have renamed them"
            ),
        )
    return HealthCheckResult(
        name="bq_datasets_reachable",
        status="pass",
        detail=f"all {len(required)} required dataset(s) reachable: {sorted(required)}",
    )


@_timed
async def _registry_tables_resolve(
    warehouse: BigQueryWarehouse, **_: Any
) -> HealthCheckResult:
    """Every logical table's body compiles, and every base table exists.

    This is the new central failure surface: the registry projection replaced
    fifteen physical tables, so a source column being renamed upstream shows up
    HERE rather than one analytics tool at a time.

    Cost: 0 bytes. Base-table existence comes from `__TABLES__` (a table that is
    absent simply has no row) rather than from INFORMATION_SCHEMA, which bills a
    10 MB minimum; body validation is the cached `SELECT * FROM (<body>) LIMIT 0`
    dry run.
    """
    counts = warehouse.base_row_counts()
    missing_bases: list[str] = []
    for name, entry in warehouse.registry.items():
        for fq in entry.base_tables:
            if fq not in counts:
                missing_bases.append(f"{name} -> {fq}")

    bad_bodies: list[str] = []
    for name in warehouse.registry:
        try:
            cols = warehouse.logical_schema(name)
        except Exception as e:
            bad_bodies.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if not cols:
            bad_bodies.append(f"{name}: body compiled but projects no columns")

    problems = [*(f"base table missing: {m}" for m in missing_bases), *bad_bodies]
    if problems:
        return HealthCheckResult(
            name="registry_tables_resolve",
            status="fail",
            detail=(
                f"{len(problems)} registry problem(s): "
                + "; ".join(problems[:6])
                + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else "")
            ),
        )

    # data_grain guard. It is an unconstrained STRING in biom_canvas, and
    # sales_weekly/inventory_weekly select specific values from it; a fourth
    # value would be silently dropped rather than reported.
    grain_note = ""
    grain_sources = sorted(
        {
            fq
            for entry in warehouse.registry.values()
            for fq in entry.base_tables
            if fq.endswith((".fct_target_sales", ".fct_target_inventory"))
        }
    )
    if grain_sources:
        union = "\nUNION ALL\n".join(
            f"SELECT DISTINCT '{fq}' AS src, data_grain FROM `{fq}`" for fq in grain_sources
        )
        try:
            _, rows = warehouse.execute_sql(union)
        except Exception as e:
            grain_note = f"; data_grain guard could not run ({type(e).__name__})"
        else:
            unexpected = sorted(
                f"{r[0]}.data_grain={r[1]!r}"
                for r in rows
                if r[1] not in EXPECTED_DATA_GRAINS
            )
            if unexpected:
                return HealthCheckResult(
                    name="registry_tables_resolve",
                    status="warn",
                    detail=(
                        f"all {len(warehouse.registry)} logical table(s) compile, but "
                        f"biom_canvas ships unexpected data_grain value(s): {unexpected} "
                        "— sales_weekly/inventory_weekly filter on an explicit list and "
                        "would silently ignore these rows"
                    ),
                )
            grain_note = f"; data_grain values as expected {sorted(EXPECTED_DATA_GRAINS)}"

    return HealthCheckResult(
        name="registry_tables_resolve",
        status="pass",
        detail=(
            f"all {len(warehouse.registry)} logical table(s) dry-run clean over "
            f"{len(counts)} base table(s), 0 bytes billed{grain_note}"
        ),
    )


@_timed
async def _roles_resolvable(warehouse: BigQueryWarehouse, **_: Any) -> HealthCheckResult:
    """Every REQUIRED_ROLES entry must resolve against every logical table.

    The most valuable check in the suite now. Under DuckDB a candidate list
    drifting from what Target ships was the failure mode; under BigQuery the
    registry's own projection is a second thing that can drift from
    COLUMN_ROLES, and this catches both. 0 bytes — it reads cached dry-run
    schemas.
    """
    from ..column_roles import validate_roles

    failures = validate_roles(warehouse)
    if not failures:
        return HealthCheckResult(
            name="roles_resolvable",
            status="pass",
            detail="all required column roles resolve on every logical table",
        )

    def _fmt(fs: list[dict[str, Any]]) -> str:
        return "; ".join(
            f"{f['dataset']}.{f['role']} (tried {f['candidates']}; "
            f"table has {f['actual_columns']})"
            for f in fs
        )

    hard = [f for f in failures if f.get("required", True)]
    soft = [f for f in failures if not f.get("required", True)]
    if hard:
        detail = (
            "unresolvable required column role(s) — the analytics tools that "
            "depend on them WILL fail. Fix by adding the real column name to "
            "column_roles.COLUMN_ROLES, or by projecting it from the logical "
            "table's body in bq.LOGICAL_TABLES: " + _fmt(hard)
        )
        if soft:
            detail += " | listing-only role(s) also unresolvable: " + _fmt(soft)
        return HealthCheckResult(name="roles_resolvable", status="fail", detail=detail)
    return HealthCheckResult(
        name="roles_resolvable",
        status="warn",
        detail=(
            "listing-only column role(s) unresolvable — bpd_list_datasets/"
            "bpd_data_freshness fall back to single-date reporting for the "
            "affected dataset(s); no analytics tool fails: " + _fmt(soft)
        ),
    )


@_timed
async def _datasets_have_data(warehouse: BigQueryWarehouse, **_: Any) -> HealthCheckResult:
    """Every logical table's primary base table has rows. 0 bytes (`__TABLES__`).

    Row counts are BASE-table counts, so they overstate filtered/deduped tables
    — which is fine for an "is it empty?" test and is why this must never be
    `COUNT(*)` through the CTE.
    """
    counts = warehouse.base_row_counts()
    rows_by_dataset = warehouse.list_datasets()
    status_by_dataset = {r["dataset"]: r.get("status") for r in rows_by_dataset}

    populated = 0
    empty_active: list[str] = []
    empty_retired: list[str] = []
    for name, entry in warehouse.registry.items():
        if counts.get(entry.primary_base_table, 0) > 0:
            populated += 1
        elif status_by_dataset.get(name) == "retired":
            empty_retired.append(name)
        else:
            empty_active.append(name)

    total = len(warehouse.registry)
    retired_note = (
        f"; empty-but-retired (feed sunset by Target, no data expected): {empty_retired}"
        if empty_retired
        else ""
    )
    if not empty_active:
        return HealthCheckResult(
            name="datasets_have_data",
            status="pass",
            detail=f"{populated}/{total} dataset(s) populated{retired_note}",
        )
    return HealthCheckResult(
        name="datasets_have_data",
        status="warn",
        detail=(
            f"{populated}/{total} dataset(s) populated; empty active dataset(s): "
            f"{empty_active} — the upstream Kiteworks -> GCS -> BigQuery pipeline "
            "(daily, ~06:47 UTC) has landed no rows for them. Check "
            f"`{warehouse.project}.{BQ_META_DATASET}.{BQ_INGESTION_STATE}` for that "
            f"table's pattern, or bpd_data_freshness{retired_note}"
        ),
    )


@_timed
async def _feed_freshness(warehouse: BigQueryWarehouse, **_: Any) -> HealthCheckResult:
    """Per-pattern lag between today and the newest file the pipeline landed.

    Reads `bpd_meta.ingestion_state` (834 rows). A pattern beyond
    RETIRED_AFTER_DAYS is reported as retired, not as a failure — Target has
    genuinely sunset several item-grain rollups.
    """
    stats = warehouse.freshness_stats()
    per_pattern = stats["per_pattern"]
    if not per_pattern:
        return HealthCheckResult(
            name="feed_freshness",
            status="fail",
            detail=(
                f"{BQ_META_DATASET}.{BQ_INGESTION_STATE} is empty — the upstream "
                "pipeline has never recorded a file"
            ),
        )
    stale: list[str] = []
    retired: list[str] = []
    for s in per_pattern:
        lag = s.get("lag_days")
        if lag is None:
            continue
        if lag > RETIRED_AFTER_DAYS:
            retired.append(f"{s['pattern']} ({lag}d)")
        elif lag > STALE_WARN_DAYS:
            stale.append(f"{s['pattern']} ({lag}d, newest file {s['max_file_date']})")
    retired_note = f"; retired (>{RETIRED_AFTER_DAYS}d): {retired}" if retired else ""
    base = (
        f"{stats['total_files']} file(s) across {stats['patterns_seen']} pattern(s); "
        f"last ingest {stats['last_ingest_at']}"
    )
    if stale:
        return HealthCheckResult(
            name="feed_freshness",
            status="warn",
            detail=(
                f"{base}; pattern(s) stale beyond {STALE_WARN_DAYS}d: {stale} — "
                "this is an upstream pipeline condition, not an MCP fault"
                f"{retired_note}"
            ),
        )
    return HealthCheckResult(
        name="feed_freshness",
        status="pass",
        detail=f"{base}; every active pattern within {STALE_WARN_DAYS}d{retired_note}",
    )


@_timed
async def _known_unpopulated_columns(
    warehouse: BigQueryWarehouse, **_: Any
) -> HealthCheckResult:
    """Reverse drift detection for KNOWN_UNPOPULATED_AT_SOURCE columns.

    These columns ship as Target's `""` NULL placeholder — a data-source fact,
    not a parser bug. The test is "at least MOSTLY placeholder", not
    "exclusively": `orders_daily.purchase_order_active_f` now holds three
    values (`'""'` x144,332, `'true'` x1,861, `''` x973), so an exclusivity test
    would warn forever.

    Do NOT respond to a warning here by filtering on the column: at 98%
    placeholder any filter drops nearly the entire order book, and
    `get_open_orders` derives openness arithmetically instead
    (ordered - received - cancel_remaining), which stays correct.
    """
    from ..column_roles import KNOWN_UNPOPULATED_AT_SOURCE

    threshold = 0.95
    newly_populated: list[str] = []
    checked = 0
    for dataset, columns in KNOWN_UNPOPULATED_AT_SOURCE.items():
        if dataset not in warehouse.registry:
            continue
        present = {n for n, _ in warehouse.logical_schema(dataset)}
        for col in columns:
            if col not in present:
                continue
            ident = quote_ident(col)
            _, rows = warehouse.execute_sql(
                f"SELECT COUNT(*) AS total, "
                f"COUNTIF({ident} IS NULL OR TRIM(CAST({ident} AS STRING)) IN ('\"\"', '')) "
                f"AS placeholder FROM {dataset}"
            )
            if not rows:
                continue
            checked += 1
            total, placeholder = int(rows[0][0] or 0), int(rows[0][1] or 0)
            if total == 0:
                continue
            ratio = placeholder / total
            if ratio < threshold:
                newly_populated.append(
                    f"{dataset}.{col} ({100 * (1 - ratio):.1f}% real values of {total:,} rows)"
                )
    if newly_populated:
        return HealthCheckResult(
            name="known_unpopulated_columns",
            status="warn",
            detail=(
                "Target has started meaningfully populating column(s) we treat as "
                "always-NULL: " + ", ".join(newly_populated)
                + " — consider promoting to a column_roles role (but do NOT start "
                "filtering on it without checking how much of the table it drops)"
            ),
        )
    return HealthCheckResult(
        name="known_unpopulated_columns",
        status="pass",
        detail=(
            f"{checked} known-unpopulated source column(s) still >={threshold:.0%} "
            "placeholder (expected)"
        ),
    )


class _DryRunWarehouse:
    """Warehouse facade whose `execute_sql` DRY-RUNS instead of executing.

    Used by the tool smoke test. It proves the SQL each tool composes actually
    compiles against BigQuery — including every dialect translation and every
    resolved column name — at 0 bytes billed, where really invoking 11 tools
    scans real data on every health check.

    Returns the dry run's own schema with ZERO rows, so tools take their
    "no data" branch. That branch is a legitimate outcome here, not a failure:
    see `_tools_smoke_test` for how results are classified.
    """

    def __init__(self, inner: BigQueryWarehouse) -> None:
        self._inner = inner
        self.compiled = 0

    def __getattr__(self, name: str) -> Any:
        # Everything not overridden below (read_only, registry, dry_run,
        # logical_schema, describe, ...) delegates to the real warehouse.
        return getattr(self._inner, name)

    def execute_sql(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        job = self._inner.dry_run(sql)
        self.compiled += 1
        return ([f.name for f in (job.schema or [])], [])


@_timed
async def _tools_smoke_test(
    warehouse: BigQueryWarehouse, settings: Settings, execute: bool = False, **_: Any
) -> HealthCheckResult:
    """Invoke every warehouse-only tool with default arguments.

    `mcp_self_check` only proves tools are REGISTERED; this proves they RUN.
    Excluded deliberately: `bpd_health_check` (recursion) and
    `bpd_export_query_to_csv` (writes a file).

    Default mode is DRY RUN (`execute=false`): each tool's SQL is compiled and
    priced by BigQuery but never executed, so the check costs nothing and can
    run as often as anyone likes. Verdicts differ between the two modes because
    a dry run legitimately yields no rows:

      execute=true   ok -> invoked | DATA_UNAVAILABLE -> skipped | else FAIL
      execute=false  ok/DATA_UNAVAILABLE -> compiled | else WARN (the SQL may
                     be fine and the tool merely unhappy with zero rows) |
                     a raised exception -> FAIL (the SQL did not compile)
    """
    from ..schemas import (
        DescribeSchemaInput,
        ForecastVsActualInput,
        InventorySnapshotInput,
        OpenOrdersInput,
        RunSqlInput,
        SalesSummaryInput,
        SellThroughInput,
        TopSkusInput,
        UpcomingPosInput,
    )
    from . import query as query_tools

    wh: Any = warehouse if execute else _DryRunWarehouse(warehouse)
    roster: list[tuple[str, Any]] = [
        ("bpd_run_sql", lambda: query_tools.run_sql(wh, RunSqlInput(sql="SELECT 1", limit=1))),
        ("bpd_describe_schema", lambda: query_tools.describe_schema(wh, DescribeSchemaInput())),
        ("bpd_get_sales_summary", lambda: query_tools.get_sales_summary(wh, SalesSummaryInput())),
        ("bpd_get_top_skus", lambda: query_tools.get_top_skus(wh, TopSkusInput(top_n=1))),
        ("bpd_get_inventory_snapshot", lambda: query_tools.get_inventory_snapshot(wh, InventorySnapshotInput(limit=1))),
        ("bpd_get_sell_through", lambda: query_tools.get_sell_through(wh, SellThroughInput())),
        ("bpd_get_open_orders", lambda: query_tools.get_open_orders(wh, OpenOrdersInput())),
        ("bpd_get_upcoming_pos", lambda: query_tools.get_upcoming_pos(wh, UpcomingPosInput())),
        ("bpd_get_forecast_vs_actual", lambda: query_tools.get_forecast_vs_actual(wh, ForecastVsActualInput())),
        ("bpd_list_datasets", lambda: list_datasets(warehouse, ListDatasetsInput())),
        ("bpd_data_freshness", lambda: data_freshness(warehouse, settings, DataFreshnessInput())),
    ]

    invoked: list[str] = []
    skipped: list[str] = []
    soft: list[str] = []
    failed: list[str] = []
    for name, call in roster:
        try:
            resp = await call()
        except Exception as e:
            failed.append(f"{name}: raised {type(e).__name__}: {e}")
            continue
        code = resp.error.code if resp.error is not None else None
        msg = resp.error.message if resp.error is not None else ""
        if resp.ok:
            invoked.append(name)
        elif code == "DATA_UNAVAILABLE":
            (invoked if not execute else skipped).append(name)
        elif execute:
            failed.append(f"{name}: {code}: {msg[:160]}")
        else:
            soft.append(f"{name}: {code}: {msg[:120]}")

    mode = "executed" if execute else "dry-run"
    if failed:
        return HealthCheckResult(
            name="tools_smoke_test",
            status="fail",
            detail=(
                f"[{mode}] {len(failed)} tool(s) broken ({len(invoked)} ok, "
                f"{len(skipped)} skipped): " + " | ".join(failed)
            ),
        )
    if soft:
        return HealthCheckResult(
            name="tools_smoke_test",
            status="warn",
            detail=(
                f"[{mode}] {len(invoked)} tool(s) compiled clean; {len(soft)} returned "
                "an error under dry run — this may only mean the tool dislikes zero "
                "rows, so re-run with execute=true to be sure: " + " | ".join(soft)
            ),
        )
    detail = f"[{mode}] {len(invoked)} tool(s) ok: {invoked}"
    if skipped:
        detail += f"; {len(skipped)} skipped (no data): {skipped}"
    if not execute:
        compiled = getattr(wh, "compiled", 0)
        detail += (
            f"; {compiled} statement(s) compiled and priced by BigQuery at 0 bytes "
            "billed (pass execute=true to really run them)"
        )
    return HealthCheckResult(name="tools_smoke_test", status="pass", detail=detail)


# ---------- runner ----------


async def health_check(
    *,
    warehouse: BigQueryWarehouse,
    settings: Settings,
    params: HealthCheckInput,
) -> ToolResponse:
    """Run all health checks and return a structured report.

    `skip_network=True` runs only the checks that make no BigQuery call, so an
    operator can still get a verdict during an outage or with no credentials.

    Metadata caches are cleared first: a health check exists to report the
    CURRENT state, and reporting a 900-second-old date range as "current" is
    the exact failure this tool must not have.
    """
    common: dict[str, Any] = {
        "warehouse": warehouse,
        "settings": settings,
        "skip_network": params.skip_network,
        "execute": params.execute,
    }
    checks: list[HealthCheckResult] = []

    # Local first, so a misconfiguration surfaces before anything waits on a
    # network round trip.
    checks.append(await _bq_credentials_present(**common))
    checks.append(await _location_configured(**common))
    checks.append(await _config_validity(**common))
    checks.append(await _mcp_self_check(**common))

    if params.skip_network:
        checks.append(
            HealthCheckResult(
                name="bigquery_checks_skipped",
                status="warn",
                detail=(
                    "skip_network=true — reachability, registry, roles, data "
                    "presence, feed freshness and the tool smoke test were not "
                    "run. Re-run without skip_network for a real verdict."
                ),
            )
        )
    else:
        warehouse.refresh_metadata()
        checks.append(await _bq_reachable_as(**common))
        checks.append(await _bq_datasets_reachable(**common))
        checks.append(await _registry_tables_resolve(**common))
        checks.append(await _roles_resolvable(**common))
        checks.append(await _datasets_have_data(**common))
        checks.append(await _feed_freshness(**common))
        checks.append(await _known_unpopulated_columns(**common))
        checks.append(await _tools_smoke_test(**common))

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
            "warehouse": warehouse.db_path,
            "smoke_test_mode": "executed" if params.execute else "dry-run",
        },
        fmt=params.response_format,
    )
