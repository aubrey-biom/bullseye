"""FastMCP server entry point.

Holds a single shared lifespan context with:
  * Settings (env)
  * AuthManager + token bundle from disk
  * httpx.AsyncClient (host-pinned)
  * KiteworksClient
  * Writable Warehouse + a read-only snapshot for `bpd_run_sql` (engine-level RO).

Each tool function takes its arguments as **top-level** parameters (not a wrapped
`params:` model) so MCP clients send flat argument dicts. The corresponding Pydantic
input models in `schemas.py` are used for validation inside each tool.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date as _date
from typing import Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP

from .auth import AuthManager
from .client import KiteworksClient, make_http_client
from .config import Settings, get_settings
from .logging_setup import configure_logging, get_logger
from .schemas import (
    AuthStatusInput,
    CacheStatusInput,
    ClearCacheInput,
    DescribeSchemaInput,
    ForecastVsActualInput,
    GetFileMetadataInput,
    InventorySnapshotInput,
    KnownDataset,
    ListDatasetsInput,
    ListFolderContentsInput,
    ListTopFoldersInput,
    OpenOrdersInput,
    RefreshDatasetInput,
    ResponseFormat,
    RunSqlInput,
    SalesSummaryInput,
    SearchFilesInput,
    SellThroughInput,
    SyncNewFilesInput,
    ToolResponse,
    TopSkusInput,
    UpcomingPosInput,
)
from .tools import admin as admin_tools
from .tools import files as files_tools
from .tools import query as query_tools
from .tools import sync as sync_tools
from .warehouse import ReadOnlySnapshot, Warehouse

logger = get_logger("bpd_mcp.server")


@dataclass
class AppContext:
    settings: Settings
    http: httpx.AsyncClient
    auth: AuthManager
    client: KiteworksClient
    warehouse_rw: Warehouse
    warehouse_ro: ReadOnlySnapshot

    async def aclose(self) -> None:
        try:
            self.warehouse_rw.close()
        finally:
            try:
                self.warehouse_ro.close()
            finally:
                await self.http.aclose()


async def build_context(settings: Settings | None = None) -> AppContext:
    s = settings or get_settings()
    s.ensure_dirs()
    configure_logging(s.bpd_log_level, s.log_dir)
    http = make_http_client(s)
    auth = AuthManager.load_from_disk(s, http)
    client = KiteworksClient(s, auth, http)
    warehouse_rw = Warehouse(s.db_path, read_only=False)
    warehouse_ro = ReadOnlySnapshot(s.db_path)
    logger.info(
        "context_built",
        base_url=s.base_url,
        vendor_id=s.bpd_vendor_id,
        tier=s.bpd_vendor_tier,
        db=str(s.db_path),
    )
    return AppContext(
        settings=s,
        http=http,
        auth=auth,
        client=client,
        warehouse_rw=warehouse_rw,
        warehouse_ro=warehouse_ro,
    )


@asynccontextmanager
async def lifespan(_server: FastMCP):
    ctx = await build_context()
    if ctx.settings.bpd_auto_sync_on_start:
        try:
            from .sync import sync_new_files as _sync_new_files

            logger.info("auto_sync_on_start")
            await _sync_new_files(
                ctx.client, ctx.warehouse_rw, ctx.settings, triggered_by="auto_sync_on_start"
            )
        except Exception as e:
            logger.warning("auto_sync_on_start_failed", error=str(e))
    try:
        yield ctx
    finally:
        await ctx.aclose()


mcp: FastMCP = FastMCP("bpd_mcp", lifespan=lifespan)


def _ctx(c: Context) -> AppContext:
    return c.request_context.lifespan_context  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_list_top_folders",
    description=(
        "List top-level Kiteworks folders. Use once during setup to find the vendor's "
        "BPD folder (named with the BPID, e.g. 139440)."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_list_top_folders(
    ctx: Context,
    limit: int = 20,
    offset: int = 0,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await files_tools.list_top_folders(
        app.client,
        ListTopFoldersInput(limit=limit, offset=offset, response_format=response_format),
    )


@mcp.tool(
    name="bpd_list_folder_contents",
    description=(
        "Paginated listing of a Kiteworks folder. Supports name_contains and "
        "extensions filters."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_list_folder_contents(
    ctx: Context,
    folder_id: str,
    name_contains: str | None = None,
    extensions: str | None = None,
    limit: int = 20,
    offset: int = 0,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await files_tools.list_folder_contents(
        app.client,
        ListFolderContentsInput(
            folder_id=folder_id,
            name_contains=name_contains,
            extensions=extensions,
            limit=limit,
            offset=offset,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_file_metadata",
    description="Return size, fingerprint, dates, and parent folder for a Kiteworks file.",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_get_file_metadata(
    ctx: Context,
    file_id: str,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await files_tools.get_file_metadata(
        app.client,
        GetFileMetadataInput(file_id=file_id, response_format=response_format),
    )


@mcp.tool(
    name="bpd_search_files",
    description="Wrap Kiteworks /rest/query for ad-hoc file/folder/content search.",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_search_files(
    ctx: Context,
    query: str,
    object_id: str | None = None,
    search_type: Literal["f", "d", "e"] = "f",
    include_content: bool = False,
    limit: int = 20,
    offset: int = 0,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await files_tools.search_files(
        app.client,
        SearchFilesInput(
            query=query,
            object_id=object_id,
            search_type=search_type,
            include_content=include_content,
            limit=limit,
            offset=offset,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_sync_new_files",
    description=(
        "Main workhorse. Discovers any new BPD zip files in the vendor's Kiteworks "
        "folder, downloads them, unzips, parses, and loads into the local DuckDB warehouse. "
        "Optional `datasets` filter restricts which file patterns to process. "
        "Set `dry_run=true` to preview without downloading."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_sync_new_files(
    ctx: Context,
    datasets: list[KnownDataset] | None = None,
    dry_run: bool = False,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await sync_tools.sync_new_files(
        app.client,
        app.warehouse_rw,
        app.settings,
        SyncNewFilesInput(
            datasets=datasets, dry_run=dry_run, response_format=response_format
        ),
    )


@mcp.tool(
    name="bpd_refresh_dataset",
    description=(
        "Re-load a single dataset. `full=true` clears the existing table and ledger for "
        "that dataset first and re-downloads everything Target has for it."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bpd_refresh_dataset(
    ctx: Context,
    dataset: KnownDataset,
    full: bool = False,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await sync_tools.refresh_dataset(
        app.client,
        app.warehouse_rw,
        app.settings,
        RefreshDatasetInput(dataset=dataset, full=full, response_format=response_format),
    )


@mcp.tool(
    name="bpd_list_datasets",
    description=(
        "Summary of every loaded BPD dataset: row count, min/max data date, file count, "
        "and last-loaded timestamp."
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
    return await sync_tools.list_datasets(
        app.warehouse_rw, ListDatasetsInput(response_format=response_format)
    )


# --------------------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_run_sql",
    description=(
        "Execute arbitrary DuckDB SQL against the local warehouse. Read-only is "
        "enforced at the engine level (a separate read_only=True connection on a "
        "DB snapshot) AND at the input-validation level (multi-statement and DDL/DML "
        "tokens rejected). Wraps the result in LIMIT to cap returned rows."
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
        app.warehouse_ro.get(),
        RunSqlInput(sql=sql, limit=limit, response_format=response_format),
    )


@mcp.tool(
    name="bpd_describe_schema",
    description=(
        "Return all tables, columns, and types in the local BPD warehouse. Also "
        "exposed as the MCP resource `bpd://schema`."
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
        app.warehouse_ro.get(), DescribeSchemaInput(response_format=response_format)
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
        app.warehouse_ro.get(),
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
        app.warehouse_ro.get(),
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
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_inventory_snapshot(
        app.warehouse_ro.get(),
        InventorySnapshotInput(
            as_of=as_of,
            tcin=tcin,
            location_id=location_id,
            limit=limit,
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
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_sell_through(
        app.warehouse_ro.get(),
        SellThroughInput(
            start_date=start_date,
            end_date=end_date,
            tcin=tcin,
            location_id=location_id,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# S&OP analytics (May 2026 patch)
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_get_open_orders",
    description=(
        "Outstanding Target POs to the vendor, summed by SKU. Reads the orders_daily "
        "table loaded from `BV_<BPID>_DAILY_ORDER_TCIN_LOC_*.zip`. Uses any of "
        "{open_units, units_remaining, qty_open, ...} if present; otherwise excludes "
        "rows whose status looks fulfilled; otherwise sums all ordered units placed "
        "on or before `as_of_date`. The chosen method is reported in `extra.method`."
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
        app.warehouse_ro.get(),
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
        "Target's planned future POs to Biom, by week and SKU. Combines po_plan_daily "
        "and po_plan_biweekly (UNION ALL after projecting to (tcin, week, qty)). The "
        "qty and date columns on each table are resolved at runtime, not hardcoded."
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
        app.warehouse_ro.get(),
        UpcomingPosInput(
            weeks_forward=weeks_forward,
            tcin_filter=tcin_filter,
            response_format=response_format,
        ),
    )


@mcp.tool(
    name="bpd_get_forecast_vs_actual",
    description=(
        "Join Target's DFE weekly forecast (forecast_weekly) with sales_weekly actuals. "
        "Returns forecast_units, actual_units, variance_units, and variance_pct per "
        "group. `aggregate` controls grouping: by_sku_week (default), "
        "by_sku_location_week (most granular), or by_sku (collapses time)."
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
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await query_tools.get_forecast_vs_actual(
        app.warehouse_ro.get(),
        ForecastVsActualInput(
            weeks_back=weeks_back,
            tcin_filter=tcin_filter,
            location_filter=location_filter,
            aggregate=aggregate,
            response_format=response_format,
        ),
    )


# --------------------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------------------


@mcp.tool(
    name="bpd_auth_status",
    description="Show Kiteworks authentication state, scope, and the user email via /rest/users/me.",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def bpd_auth_status(
    ctx: Context, response_format: ResponseFormat = "markdown"
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.auth_status(
        app.auth, app.client, AuthStatusInput(response_format=response_format)
    )


@mcp.tool(
    name="bpd_cache_status",
    description=(
        "Disk usage, row counts, oldest/newest data dates, and last sync time for the "
        "local BPD cache."
    ),
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bpd_cache_status(
    ctx: Context, response_format: ResponseFormat = "markdown"
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.cache_status(
        app.warehouse_rw, app.settings, CacheStatusInput(response_format=response_format)
    )


@mcp.tool(
    name="bpd_clear_cache",
    description=(
        "Destructive. Wipes raw zips, extracted files, and the DuckDB warehouse. "
        "Requires `confirm=true`; otherwise returns a dry-run preview of what would "
        "be deleted."
    ),
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bpd_clear_cache(
    ctx: Context,
    confirm: bool = False,
    response_format: ResponseFormat = "markdown",
) -> ToolResponse:
    app = _ctx(ctx)
    return await admin_tools.clear_cache(
        app.warehouse_rw,
        app.settings,
        ClearCacheInput(confirm=confirm, response_format=response_format),
    )


# --------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------


@mcp.resource("bpd://schema", description="The current DuckDB warehouse schema as markdown.")
async def bpd_schema_resource() -> str:
    # Resources don't see the lifespan context; open a fresh read-only handle on the
    # snapshot file (or the source DB itself if no writable connection is open).
    s = get_settings()
    snap = ReadOnlySnapshot(s.db_path)
    try:
        wh = snap.get()
        resp = await query_tools.describe_schema(
            wh, DescribeSchemaInput(response_format="markdown")
        )
        return resp.rendered
    finally:
        snap.close()


def run() -> None:
    """Synchronous entry point invoked by the `bpd-mcp` console script."""
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("bpd-mcp: shutdown", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run())
