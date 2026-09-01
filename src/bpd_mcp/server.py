"""FastMCP server entry point.

The lifespan context holds exactly two things now:

  * `Settings` (env)
  * one `BigQueryWarehouse` — a read-only, network-backed data layer

**Why that matters.** The previous design opened a local DuckDB file. DuckDB
allows exactly ONE process to hold a database file: while a read-write
connection is live, a second process cannot open it at all, not even with
`read_only=True`. Claude Desktop now spawns a second copy of this server for
Cowork/Code sessions, and that second copy crashed on the lock. BigQuery is a
network service, so N processes can hold N clients with no contention — that
is the entire point of the change.

Consequently, **nothing in startup may reintroduce a single-process
assumption**. No file locks, no PID/state file, no snapshot copy, no leftover
`.ro` cleanup (which raced two processes against the same unlink), no
auto-sync-on-start writing to shared local state. The only local paths the
server touches at all are its own log directory and the CSV `exports/`
directory, both of which are append-only outputs whose worst-case concurrent
behaviour is an interleaved log line.

Each tool function takes its arguments as **top-level** parameters (not a
wrapped `params:` model) so MCP clients send flat argument dicts. The
corresponding Pydantic input models in `schemas.py` are used for validation
inside each tool.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date as _date
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

# Imported from `.bq` rather than `.warehouse`: `bq` is where the BigQuery data
# layer actually lives, and `warehouse` is only a compatibility re-export kept
# so `tools/query.py`'s `from ..warehouse import Warehouse, quote_ident` and its
# `warehouse: Warehouse` annotations keep resolving.
from .bq import BigQueryWarehouse
from .config import Settings, get_settings
from .logging_setup import configure_logging, get_logger
from .schemas import (
    BigQueryStatusInput,
    DataFreshnessInput,
    DescribeSchemaInput,
    ExportQueryToCsvInput,
    ForecastVsActualInput,
    HealthCheckInput,
    InventorySnapshotInput,
    ListDatasetsInput,
    OpenOrdersInput,
    ResponseFormat,
    RunSqlInput,
    SalesSummaryInput,
    SellThroughInput,
    ToolResponse,
    TopSkusInput,
    UpcomingPosInput,
)
from .tools import admin as admin_tools
from .tools import query as query_tools

logger = get_logger("bpd_mcp.server")


@dataclass
class AppContext:
    """Everything a tool call needs. Two fields, both process-local and shareable.

    There is no writable/read-only warehouse pair any more. The old design
    needed one because DuckDB read-only enforcement was a transaction wrapper
    around a writable connection; here the service account holds
    `dataViewer + jobUser` and gets a 403 on `bigquery.tables.create`, so
    read-only is a property of the credential and cannot be turned off by a
    code path.
    """

    settings: Settings
    warehouse: BigQueryWarehouse

    async def aclose(self) -> None:
        global _active_app_context
        try:
            # Idempotent and never raises. Nothing to unlink: there is no file,
            # no lock, and no snapshot — so a second server process closing at
            # the same moment cannot affect this one.
            self.warehouse.close()
        finally:
            if _active_app_context is self:
                _active_app_context = None


# FastMCP resources don't receive the lifespan context, so we keep a module-level
# reference set by build_context() and cleared by AppContext.aclose(). Used by the
# `bpd://schema` resource. This is per-PROCESS state, not cross-process state —
# two concurrently running servers each hold their own and never interact.
_active_app_context: AppContext | None = None


async def build_context(settings: Settings | None = None) -> AppContext:
    """Construct the app context. Safe to run in several processes at once.

    Deliberately absent, and each one is a single-process assumption that used
    to live here:

      * `cleanup_legacy_snapshot()` — unlinked `bpd.duckdb.ro`; two servers
        starting together raced on the same path.
      * `Warehouse(db_path, read_only=False)` — took the DuckDB file lock, the
        actual reported symptom (the second server would not start).
      * `ReadOnlyView(...)` — a facade over that same locked handle.
      * `make_http_client` / `AuthManager.load_from_disk` / `KiteworksClient` —
        read and rewrote `~/.bpd-mcp/tokens.json`, so a refresh in one process
        invalidated the other's in-flight token.
      * the `bpd_auto_sync_on_start` branch in `lifespan` — two servers booting
        together would both start downloading and writing.

    What remains is a BigQuery client (a stateless HTTPS session) plus two
    output directories.
    """
    global _active_app_context
    s = settings or get_settings()
    s.ensure_dirs()
    configure_logging(s.bpd_log_level, s.log_dir)

    warehouse = BigQueryWarehouse(
        project=s.bpd_bq_project,
        location=s.bpd_bq_location,
        maximum_bytes_billed=s.bpd_bq_max_bytes_billed,
        rowcount_ttl_s=s.bpd_bq_rowcount_ttl_s,
        daterange_ttl_s=s.bpd_bq_daterange_ttl_s,
    )

    # Log-only startup pass over the role registry. Never fatal, but the reason
    # changed: under DuckDB, tables were created lazily by sync, so a fresh
    # install legitimately had nothing to validate. Now every logical table
    # exists at boot, so an unresolvable role means the registry projection and
    # COLUMN_ROLES have genuinely drifted. It stays a warning here (a boot-time
    # BigQuery outage must not make the server unstartable) and is a hard gate
    # in the `roles_resolvable` health check.
    try:
        from .column_roles import validate_roles

        for failure in validate_roles(warehouse):
            logger.warning("role_unresolvable", **failure)
    except Exception as e:
        logger.warning("role_validation_failed", error=str(e))

    logger.info(
        "context_built",
        warehouse=warehouse.db_path,
        project=s.bpd_bq_project,
        location=s.bpd_bq_location,
        credentials_source=warehouse.credentials_source,
        logical_tables=len(warehouse.registry),
        vendor_id=s.bpd_vendor_id,
        tier=s.bpd_vendor_tier,
    )
    ctx = AppContext(settings=s, warehouse=warehouse)
    _active_app_context = ctx
    return ctx


@asynccontextmanager
async def lifespan(_server: FastMCP):
    ctx = await build_context()
    try:
        yield ctx
    finally:
        await ctx.aclose()


mcp: FastMCP = FastMCP("bpd_mcp", lifespan=lifespan)


def _ctx(c: Context) -> AppContext:
    return c.request_context.lifespan_context  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_list_datasets",
    description=(
        "Summary of every BPD dataset queryable in BigQuery: row count, snapshot "
        "date range (freshness), content date range (how far order_d / fiscal "
        "weeks / ETAs reach), the number of source files the upstream pipeline "
        "has landed, and when it last landed one."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_list_datasets(
    ctx: Context,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.list_datasets(
        app.warehouse, ListDatasetsInput(response_format=response_format)
    )


# --------------------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_run_sql",
    description=(
        "Execute arbitrary BigQuery Standard SQL against the BPD logical tables "
        "(sales_daily, sales_weekly, inventory_daily, orders_daily, "
        "forecast_weekly, ... — see bpd_describe_schema). Reference them by bare "
        "name; the server injects each referenced table as a CTE. Read-only is "
        "enforced at the credential layer (the service account holds dataViewer "
        "+ jobUser and cannot create or write anything) AND at the input "
        "validator (multi-statement and DDL/DML tokens rejected). Every query is "
        "dry-run first for cost, and the result is wrapped in LIMIT."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_run_sql(
    ctx: Context,
    sql: str,
    limit: int = 200,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.run_sql(
        app.warehouse,
        RunSqlInput(sql=sql, limit=limit, response_format=response_format),
    )


@mcp.tool(
    name="bpd_export_query_to_csv",
    description=(
        "Run a read-only SQL query and write the result to a CSV file in "
        "~/.bpd-mcp/exports/<filename>. Useful for sharing analytical results "
        "with team members who don't have MCP access. Same read-only safety and "
        "cost gate as bpd_run_sql. Returns the absolute path so the user can "
        "open the file in Finder."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bpd_export_query_to_csv(
    ctx: Context,
    sql: str,
    filename: str,
    include_header: bool = True,
    max_rows: int | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    # Default comes from settings (BPD_EXPORT_MAX_ROWS, 200k) rather than being
    # hardcoded: on per-byte billing an unguarded export is a money question,
    # not a disk question.
    return await query_tools.export_query_to_csv(
        app.warehouse,
        app.settings,
        ExportQueryToCsvInput(
            sql=sql,
            filename=filename,
            include_header=include_header,
            max_rows=max_rows if max_rows is not None else app.settings.bpd_export_max_rows,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_describe_schema",
    description=(
        "Return every BPD logical table, its columns and types, the BigQuery base "
        "table behind it, and any latest-state reduction applied. Also exposed as "
        "the MCP resource `bpd://schema`."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_describe_schema(
    ctx: Context, response_format: ResponseFormat = "markdown"
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.describe_schema(
        app.warehouse, DescribeSchemaInput(response_format=response_format)
    )


@mcp.tool(
    name="bpd_get_sales_summary",
    description=(
        "Aggregate sales by grain (day/week/month). Optional date range and TCIN/"
        "location filters. Returns total units (and dollars when the schema has a "
        "dollar column)."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_sales_summary(
    ctx: Context,
    grain: Literal["day", "week", "month"] = "week",
    start_date: _date | None = None,
    end_date: _date | None = None,
    tcin: int | None = None,
    location_id: int | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_sales_summary(
        app.warehouse,
        SalesSummaryInput(
            grain=grain,
            start_date=start_date,
            end_date=end_date,
            tcin=tcin,
            location_id=location_id,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_top_skus",
    description="Top N SKUs by units or dollars over a date range, ordered descending.",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_top_skus(
    ctx: Context,
    by: Literal["units", "dollars"] = "units",
    start_date: _date | None = None,
    end_date: _date | None = None,
    top_n: int = 20,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_top_skus(
        app.warehouse,
        TopSkusInput(
            by=by,
            start_date=start_date,
            end_date=end_date,
            top_n=top_n,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_inventory_snapshot",
    description=(
        "Latest known inventory per TCIN × location at or before a date. Defaults to "
        "today. Uses inventory_daily if available, else inventory_weekly."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_inventory_snapshot(
    ctx: Context,
    as_of: _date | None = None,
    tcin: int | None = None,
    location_id: int | None = None,
    limit: int = 200,
    max_staleness_days: int | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_inventory_snapshot(
        app.warehouse,
        InventorySnapshotInput(
            as_of=as_of,
            tcin=tcin,
            location_id=location_id,
            limit=limit,
            max_staleness_days=max_staleness_days,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_sell_through",
    description=(
        "Joins weekly sales and latest inventory to compute weeks-of-supply and "
        "sell-through rate per TCIN × location."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_sell_through(
    ctx: Context,
    start_date: _date | None = None,
    end_date: _date | None = None,
    tcin: int | None = None,
    location_id: int | None = None,
    max_staleness_days: int | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_sell_through(
        app.warehouse,
        SellThroughInput(
            start_date=start_date,
            end_date=end_date,
            tcin=tcin,
            location_id=location_id,
            max_staleness_days=max_staleness_days,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# S&OP analytics
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_get_open_orders",
    description=(
        "Outstanding Target POs to the vendor, summed by SKU. orders_daily is "
        "reduced to a latest-state order book (one row per PO line, newest "
        "snapshot_d wins); open units are DERIVED as revised_order_q - "
        "item_received_q - cancel_remaining_order_q, keeping lines > 0. "
        "`as_of_date` filters by PO creation date (not time travel). "
        "Returns po_count, open_units, line_count per TCIN; derivation in `extra`."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_open_orders(
    ctx: Context,
    as_of_date: _date | None = None,
    location_filter: list[int] | None = None,
    tcin_filter: list[int] | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_open_orders(
        app.warehouse,
        OpenOrdersInput(
            as_of_date=as_of_date,
            location_filter=location_filter,
            tcin_filter=tcin_filter,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_upcoming_pos",
    description=(
        "Target's planned future POs to Biom, by week and SKU. Reads po_plan_daily "
        "and po_plan_biweekly, each filtered to its LATEST business_d snapshot (the "
        "tables accumulate a full plan snapshot per day). Windows on order_d (the "
        "planned order date) and groups by (tcin, week, source) — daily and biweekly "
        "plans are reported separately, with per-source totals and snapshot ages in "
        "`extra`. When the two plans diverge, prefer po_plan_daily for the near "
        "horizon (fresher snapshot); use po_plan_biweekly only beyond its reach."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_upcoming_pos(
    ctx: Context,
    weeks_forward: int = 8,
    tcin_filter: list[int] | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_upcoming_pos(
        app.warehouse,
        UpcomingPosInput(
            weeks_forward=weeks_forward,
            tcin_filter=tcin_filter,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_forecast_vs_actual",
    description=(
        "Join Target's DFE weekly forecast (forecast_weekly) with sales_weekly actuals "
        "on a coverage-honest (tcin, location, week) spine: only MATCHED cells produce "
        "variance; unmatched forecast/actual volume is counted in extra.coverage (and "
        "returned as rows only with include_unmatched=true) — never zero-filled. "
        "variance_pct is a true percent (0-100 scale). weeks_back is clamped to actuals "
        "coverage with the effective range echoed. snapshot_policy picks "
        "latest_available (default) or pre_week forecast snapshots; extra.forecast_drops "
        "classifies each snapshot as weekly_retrospective vs forward_horizon."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_get_forecast_vs_actual(
    ctx: Context,
    weeks_back: int = 12,
    tcin_filter: list[int] | None = None,
    location_filter: list[int] | None = None,
    aggregate: Literal["by_sku_week", "by_sku_location_week", "by_sku"] = "by_sku_week",
    snapshot_policy: Literal["latest_available", "pre_week"] = "latest_available",
    include_unmatched: bool = False,
    pre_week_min_lead_days: int = 1,
    as_of_date: _date | None = None,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_forecast_vs_actual(
        app.warehouse,
        ForecastVsActualInput(
            weeks_back=weeks_back,
            tcin_filter=tcin_filter,
            location_filter=location_filter,
            aggregate=aggregate,
            snapshot_policy=snapshot_policy,
            include_unmatched=include_unmatched,
            pre_week_min_lead_days=pre_week_min_lead_days,
            as_of_date=as_of_date,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_bigquery_status",
    description=(
        "Show which BigQuery identity this server is querying as (SESSION_USER()), "
        "where the credential came from, the project and location it is pinned to, "
        "which datasets are reachable, and the fact that the credential has NO write "
        "capability. Replaces the old bpd_auth_status."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_bigquery_status(
    ctx: Context, response_format: ResponseFormat = "markdown"
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.bigquery_status(
        app.warehouse, BigQueryStatusInput(response_format=response_format)
    )


@mcp.tool(
    name="bpd_data_freshness",
    description=(
        "How current is the BPD data? Per-dataset snapshot and content date ranges "
        "plus, from the upstream pipeline's own ledger (bpd_meta.ingestion_state), "
        "per-pattern file counts, newest file date, last download time and lag in "
        "days. Replaces the old bpd_cache_status (which measured local disk). Note "
        "that a recent download means a FILE arrived, not that rows are queryable — "
        "the per-dataset max_date is the authority on that."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_data_freshness(
    ctx: Context, response_format: ResponseFormat = "markdown"
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.data_freshness(
        app.warehouse, app.settings, DataFreshnessInput(response_format=response_format)
    )


@mcp.tool(
    name="bpd_health_check",
    description=(
        "Run a comprehensive multi-check audit across BigQuery credentials and "
        "reachability, the logical-table registry, the column-role registry, "
        "upstream feed freshness, and MCP self-state. Each check returns "
        "pass/warn/fail with a human-readable detail. The aggregate "
        "`overall_status` is `fail` if any check fails, `warn` if any warns and "
        "none fail, else `pass`. Use this as the first call when diagnosing any "
        "MCP issue. `skip_network=true` limits it to checks that need no "
        "BigQuery call; `execute=true` makes the tool smoke test really run its "
        "queries (billing bytes) instead of only dry-running them."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_health_check(
    ctx: Context,
    skip_network: bool = False,
    execute: bool = False,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.health_check(
        warehouse=app.warehouse,
        settings=app.settings,
        params=HealthCheckInput(
            skip_network=skip_network,
            execute=execute,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------


@mcp.resource(
    "bpd://schema",
    description="The BPD logical-table schema (BigQuery-backed) as markdown.",
)
async def bpd_schema_resource() -> str:
    # FastMCP resources don't receive Context; reach into the module-level
    # AppContext singleton set by build_context. Keep it: without it this
    # resource has no way to find the live warehouse and would return the
    # placeholder forever.
    if _active_app_context is None:
        return "_(MCP server context not initialized yet — try again in a moment)_"
    resp = await query_tools.describe_schema(
        _active_app_context.warehouse, DescribeSchemaInput(response_format="markdown")
    )
    return resp.rendered


def run() -> None:
    """Synchronous entry point invoked by the `bpd-mcp` console script."""
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("bpd-mcp: shutdown", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run())
