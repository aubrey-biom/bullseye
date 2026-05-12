"""Pydantic input/output schemas for every MCP tool.

Every list-style tool returns the standard envelope (§9):
    {items, total, count, offset, has_more, next_offset}

Every action-style tool returns an explicit Output model so FastMCP can publish
`outputSchema` to clients.
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


class ListEnvelope(_BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    count: int = 0
    offset: int = 0
    has_more: bool = False
    next_offset: int | None = None


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
# Files tools
# --------------------------------------------------------------------------------------


class ListTopFoldersInput(_BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    response_format: ResponseFormat = "markdown"


class ListFolderContentsInput(_BaseModel):
    folder_id: str = Field(description="UUID (or numeric string) of the folder to list.")
    name_contains: str | None = Field(
        default=None,
        description="Optional name filter passed through to Kiteworks.",
    )
    extensions: str | None = Field(
        default=None,
        description="Comma-separated list of extensions, e.g. 'zip,csv'.",
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    response_format: ResponseFormat = "markdown"


class GetFileMetadataInput(_BaseModel):
    file_id: str
    response_format: ResponseFormat = "markdown"


class SearchFilesInput(_BaseModel):
    query: str = Field(min_length=1, description="Search query.")
    object_id: str | None = Field(default=None, description="Limit to a folder UUID.")
    search_type: Literal["f", "d", "e"] = Field(
        default="f", description="'f' file, 'd' folder, 'e' email."
    )
    include_content: bool = Field(
        default=False, description="If true, run a full-text search; else metadata-only."
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# Sync tools
# --------------------------------------------------------------------------------------

KnownDataset = Literal[
    "sales_daily",
    "inventory_daily",
    "sales_weekly",
    "inventory_weekly",
    "item_attr",
    "location_attr",
    "gross_margin",
]


class SyncNewFilesInput(_BaseModel):
    datasets: list[KnownDataset] | None = Field(
        default=None,
        description="If supplied, restrict the sync to these dataset names.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, list which files would be processed without downloading.",
    )
    response_format: ResponseFormat = "markdown"


class FileOutcomeOut(_BaseModel):
    file_id: str
    file_name: str
    dataset: str | None
    status: str
    rows: int = 0
    bytes: int = 0
    error: str | None = None


class SyncNewFilesOutput(_BaseModel):
    folder_id: str | None
    files_found: int
    files_new: int
    files_loaded: int
    files_failed: int
    files_skipped: int
    files_unknown: int
    duration_s: float
    outcomes: list[FileOutcomeOut]
    notes: str = ""


class RefreshDatasetInput(_BaseModel):
    dataset: KnownDataset
    full: bool = Field(
        default=False,
        description="If true, clear the existing table+ledger for this dataset first.",
    )
    response_format: ResponseFormat = "markdown"


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
    response_format: ResponseFormat = "markdown"


class SellThroughInput(_BaseModel):
    start_date: _date | None = None
    end_date: _date | None = None
    tcin: int | None = None
    location_id: int | None = None
    response_format: ResponseFormat = "markdown"


class DescribeSchemaInput(_BaseModel):
    response_format: ResponseFormat = "markdown"


# --------------------------------------------------------------------------------------
# Admin tools
# --------------------------------------------------------------------------------------


class AuthStatusInput(_BaseModel):
    response_format: ResponseFormat = "markdown"


class CacheStatusInput(_BaseModel):
    response_format: ResponseFormat = "markdown"


class ClearCacheInput(_BaseModel):
    confirm: bool = Field(
        default=False,
        description="Must be true to actually wipe. Else returns a dry-run preview.",
    )
    response_format: ResponseFormat = "markdown"
