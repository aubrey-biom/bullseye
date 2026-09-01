"""Pydantic input/output schemas for every MCP tool.

Every tool returns a `ToolResponse`. Action-style tools additionally define an
explicit Output model so FastMCP can publish `outputSchema` to clients.

The paginated `ListEnvelope` is gone with the Kiteworks file-browsing tools —
nothing the server exposes now is a paginated remote listing.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResponseFormat = Literal["markdown", "json"]


class _BaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# Generic envelopes
# --------------------------------------------------------------------------------------


class ErrorPayload(_BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ToolResponse(_BaseModel):
    """Wrapper returned by every tool. `format` toggles markdown vs json rendering."""

    ok: bool = True
    format: ResponseFormat = "markdown"
    rendered: str = ""
    data: dict[str, Any] | None = None
    error: ErrorPayload | None = None


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------

# The 15 logical tables the server exposes. This list MUST equal
# `bq.KNOWN_DATASET_NAMES` (i.e. the keys of `bq.LOGICAL_TABLES`), which is in
# turn pinned to `column_roles.COLUMN_ROLES` / `DATASET_KINDS` / `FEED_KINDS` by
# a drift guard. It is spelled out literally rather than generated so MCP
# clients get a real enum in the published tool schema.
KnownDataset = Literal[
    "sales_daily",
    "sales_weekly",
    "sales_weekly_item",
    "inventory_daily",
    "inventory_weekly",
    "inventory_weekly_item",
    "gross_margin",
    "gross_margin_item",
    "item_attr",
    "item_attr_extended",
    "location_attr",
    "orders_daily",
    "po_plan_daily",
    "po_plan_biweekly",
    "forecast_weekly",
]


class ListDatasetsInput(_BaseModel):
    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# Query tools
# --------------------------------------------------------------------------------------


class RunSqlInput(_BaseModel):
    sql: str = Field(min_length=1, description="A single SELECT/WITH statement.")
    limit: int = Field(
        default=200,
        ge=1,
        le=10_000,
        description="Hard row cap applied via LIMIT wrapping.",
    )
    response_format: ResponseFormat = "markdown"


class ExportQueryToCsvInput(_BaseModel):
    sql: str = Field(
        min_length=1,
        description="The SQL query to execute (must be read-only — same safety as bpd_run_sql).",
    )
    filename: str = Field(
        min_length=1,
        description=(
            "Output filename. Must end in .csv and contain no path separators "
            "(no /, no \\). Saved to ~/.bpd-mcp/exports/<filename>."
        ),
    )
    include_header: bool = Field(
        default=True, description="Include column headers as the first row."
    )
    max_rows: int = Field(
        default=200_000,
        ge=1,
        le=10_000_000,
        description=(
            "Hard row cap to avoid run-away exports. Lowered from the DuckDB-era "
            "1,000,000: on per-byte billing an unguarded export is a money "
            "question, not a disk question. Overridable via BPD_EXPORT_MAX_ROWS."
        ),
    )
    response_format: ResponseFormat = "markdown"


class ExportQueryToCsvOutput(_BaseModel):
    path: str
    rows_written: int
    columns: list[str]
    bytes_written: int


class SalesSummaryInput(_BaseModel):
    grain: Literal["day", "week", "month"] = "week"
    start_date: _date | None = None
    end_date: _date | None = None
    tcin: int | None = Field(default=None, description="Restrict to a single TCIN.")
    location_id: int | None = Field(
        default=None, description="Restrict to a single location/store."
    )
    response_format: ResponseFormat = "markdown"


class TopSkusInput(_BaseModel):
    by: Literal["units", "dollars"] = "units"
    start_date: _date | None = None
    end_date: _date | None = None
    top_n: int = Field(default=20, ge=1, le=200)
    response_format: ResponseFormat = "markdown"


class InventorySnapshotInput(_BaseModel):
    as_of: _date | None = Field(
        default=None,
        description="Latest known inventory at or before this date. Defaults to today.",
    )
    tcin: int | None = None
    location_id: int | None = None
    limit: int = Field(default=200, ge=1, le=10_000)
    max_staleness_days: int | None = Field(
        default=None,
        ge=0,
        le=365,
        description=(
            "Exclude (tcin, location) pairs whose latest snapshot is more than "
            "this many days older than the table's newest date. The tool "
            "carries forward 'latest known' per pair, so daily-feed gaps can "
            "surface weeks-old on-hand as if current (Patch #12); extra."
            "staleness reports how much of the result is stale either way."
        ),
    )
    response_format: ResponseFormat = "markdown"


class SellThroughInput(_BaseModel):
    start_date: _date | None = None
    end_date: _date | None = None
    tcin: int | None = None
    location_id: int | None = None
    max_staleness_days: int | None = Field(
        default=None,
        ge=0,
        le=365,
        description=(
            "Exclude inventory pairs whose latest snapshot is more than this "
            "many days older than the inventory table's newest date — "
            "weeks-of-supply computed from 10-week-old on-hand is misleading "
            "(Patch #12)."
        ),
    )
    response_format: ResponseFormat = "markdown"


class DescribeSchemaInput(_BaseModel):
    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# S&OP analytics (May 2026 patch)
# --------------------------------------------------------------------------------------


class OpenOrdersInput(_BaseModel):
    as_of_date: _date | None = Field(
        default=None,
        description=(
            "Only count POs CREATED on or before this date "
            "(purchase_order_create_d). The order book is latest-state per PO "
            "line, so this is not a historical reconstruction. Default: the "
            "whole current order book."
        ),
    )
    location_filter: list[int] | None = Field(
        default=None,
        description="Restrict to these store/location IDs.",
    )
    tcin_filter: list[int] | None = Field(
        default=None, description="Restrict to these TCINs."
    )
    response_format: ResponseFormat = "markdown"


class UpcomingPosInput(_BaseModel):
    weeks_forward: int = Field(
        default=8,
        ge=1,
        le=52,
        description="How many weeks past `today` to include.",
    )
    tcin_filter: list[int] | None = Field(
        default=None, description="Restrict to these TCINs."
    )
    response_format: ResponseFormat = "markdown"


class ForecastVsActualInput(_BaseModel):
    weeks_back: int = Field(
        default=12,
        ge=1,
        le=104,
        description="How many weeks of history to compare. Anchored at today.",
    )
    tcin_filter: list[int] | None = Field(
        default=None, description="Restrict to these TCINs."
    )
    location_filter: list[int] | None = Field(
        default=None, description="Restrict to these store/location IDs."
    )
    aggregate: Literal["by_sku_week", "by_sku_location_week", "by_sku"] = Field(
        default="by_sku_week",
        description=(
            "How to aggregate the join. `by_sku_week` rolls up across locations; "
            "`by_sku_location_week` is the most granular; `by_sku` collapses time."
        ),
    )
    snapshot_policy: Literal["latest_available", "pre_week"] = Field(
        default="latest_available",
        description=(
            "Which forecast snapshot to compare (Patch #11). 'latest_available' "
            "(default): the newest snapshot per (tcin, location, week) — ingest "
            "retains exactly one per key anyway, since last_update_d is not in "
            "the natural key. 'pre_week': only snapshots published BEFORE each "
            "week began (Target's true pre-week prediction); weeks whose "
            "forecast only exists post-hoc become unmatched instead of being "
            "zero-filled."
        ),
    )
    include_unmatched: bool = Field(
        default=False,
        description=(
            "Also return forecast-only / actual-only rows (with the missing "
            "side NULL, never fabricated as 0). Unmatched volume is always "
            "counted in extra.coverage regardless."
        ),
    )
    pre_week_min_lead_days: int = Field(
        default=1,
        ge=-6,
        le=91,
        description=(
            "Only with snapshot_policy='pre_week': minimum days the snapshot "
            "must precede each week's begin. Default 1 = strictly before the "
            "week starts. Target's live forward drops publish the Monday "
            "AFTER the Sunday week-begin, so 1 excludes the same-week drop by "
            "design; use 7 for a full-week lead ('their prediction a week "
            "out'), or -1 to tolerate the Monday-after drop (leaks one day of "
            "actuals into 'pre-week')."
        ),
    )
    as_of_date: _date | None = Field(
        default=None,
        description=(
            "Explicit forecast snapshot cutoff: only snapshots with "
            "last_update_d <= as_of_date are considered (latest within the "
            "window wins). Overrides snapshot_policy."
        ),
    )
    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# Admin tools
# --------------------------------------------------------------------------------------


class BigQueryStatusInput(_BaseModel):
    """Input for `bpd_bigquery_status` (was `bpd_auth_status`)."""

    response_format: ResponseFormat = "markdown"


class DataFreshnessInput(_BaseModel):
    """Input for `bpd_data_freshness` (was `bpd_cache_status`).

    The old tool measured local disk; there is no local data store any more.
    What it was actually for — "how current is this data?" — is what survives.
    """

    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# Health check (Patch #3)
# --------------------------------------------------------------------------------------


CheckStatus = Literal["pass", "warn", "fail"]


class HealthCheckResult(_BaseModel):
    name: str
    status: CheckStatus
    detail: str
    duration_ms: int = 0


class HealthCheckInput(_BaseModel):
    skip_network: bool = Field(
        default=False,
        description=(
            "Skip every check that calls BigQuery. Leaves only the local "
            "checks (credentials present, config validity, MCP self-check). "
            "Useful when diagnosing an outage or working offline."
        ),
    )
    execute: bool = Field(
        default=False,
        description=(
            "Make the tool smoke test really RUN its queries instead of only "
            "dry-running them. A dry run proves the SQL compiles and every "
            "column role resolves at 0 bytes billed; executing scans real data "
            "across 11 tools and costs money. Default false."
        ),
    )
    response_format: ResponseFormat = "markdown"
