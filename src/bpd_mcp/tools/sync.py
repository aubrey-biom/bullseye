"""Sync tools: sync_new_files, refresh_dataset, list_datasets."""

from __future__ import annotations

from dataclasses import asdict

from ..client import KiteworksClient
from ..config import Settings
from ..formatting import (
    make_error_response,
    make_kv_response,
    make_table_response,
)
from ..schemas import (
    ListDatasetsInput,
    RefreshDatasetInput,
    SyncNewFilesInput,
    ToolResponse,
)
from ..sync import refresh_dataset as _refresh
from ..sync import sync_new_files as _sync
from ..warehouse import Warehouse


def _outcome_row(o) -> dict:
    d = asdict(o)
    # Trim noisy field for table rendering.
    if d.get("error") and len(d["error"]) > 120:
        d["error"] = d["error"][:117] + "..."
    return d


async def sync_new_files(
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    params: SyncNewFilesInput,
) -> ToolResponse:
    try:
        result = await _sync(
            client,
            warehouse,
            settings,
            datasets=params.datasets,
            triggered_by="bpd_sync_new_files",
            dry_run=params.dry_run,
        )
    except Exception as e:
        return make_error_response(
            code="SYNC_FAILED",
            message=str(e),
            fmt=params.response_format,
        )

    payload = {
        **result.summary(),
        "outcomes": [_outcome_row(o) for o in result.outcomes],
    }
    if params.response_format == "json":
        return make_table_response(
            rows=payload["outcomes"],
            extra={k: v for k, v in payload.items() if k != "outcomes"},
            title="Sync results",
            fmt="json",
        )
    return make_table_response(
        rows=payload["outcomes"],
        columns=["file_name", "dataset", "status", "rows", "bytes", "error"],
        extra={k: v for k, v in payload.items() if k != "outcomes"},
        title=(
            f"Sync results — found={result.files_found}, loaded={result.files_loaded}, "
            f"failed={result.files_failed}, skipped={result.files_skipped}, "
            f"unknown={result.files_unknown}"
        ),
        fmt="markdown",
    )


async def refresh_dataset(
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    params: RefreshDatasetInput,
) -> ToolResponse:
    try:
        result = await _refresh(
            client,
            warehouse,
            settings,
            dataset=params.dataset,
            full=params.full,
            triggered_by="bpd_refresh_dataset",
        )
    except KeyError as e:
        return make_error_response(
            code="UNKNOWN_DATASET",
            message=str(e),
            fmt=params.response_format,
        )
    except Exception as e:
        return make_error_response(
            code="REFRESH_FAILED",
            message=str(e),
            fmt=params.response_format,
        )

    payload = result.summary()
    payload["dataset"] = params.dataset
    payload["full"] = params.full
    return make_kv_response(
        data=payload, title=f"Refreshed {params.dataset}", fmt=params.response_format
    )


async def list_datasets(warehouse: Warehouse, params: ListDatasetsInput) -> ToolResponse:
    rows = warehouse.list_datasets()
    return make_table_response(
        rows=rows,
        columns=["dataset", "row_count", "min_date", "max_date", "file_count", "last_loaded_at"],
        title="Loaded BPD datasets",
        fmt=params.response_format,
    )
