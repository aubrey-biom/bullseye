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
    ReingestLocalInput,
    SyncNewFilesInput,
    ToolResponse,
)
from ..sync import CONFIRM_DESTRUCTIVE_PHRASE, RefreshWouldLoseHistory
from ..sync import refresh_dataset as _refresh
from ..sync import reingest_local_files as _reingest
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


async def reingest_local(
    warehouse: Warehouse,
    settings: Settings,
    params: ReingestLocalInput,
) -> ToolResponse:
    """Load BPD zips already in the local raw archive — no downloads (Patch #9)."""
    try:
        result = await _reingest(
            warehouse,
            settings,
            datasets=set(params.datasets) if params.datasets else None,
            only_unledgered=params.only_unledgered,
            dry_run=params.dry_run,
            triggered_by="bpd_reingest_local",
        )
    except Exception as e:
        return make_error_response(
            code="REINGEST_FAILED",
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
            title="Local reingest results",
            fmt="json",
        )
    return make_table_response(
        rows=payload["outcomes"],
        columns=["file_name", "dataset", "status", "rows", "bytes", "error"],
        extra={k: v for k, v in payload.items() if k != "outcomes"},
        title=(
            f"Local reingest — found={result.files_found}, loaded={result.files_loaded}, "
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
    # Patch #9: full=true is destructive (Kiteworks retains ~2 weeks; older
    # local history cannot be re-downloaded). Require the exact confirmation
    # phrase, and show what would be deleted.
    if params.full and params.confirm_destructive != CONFIRM_DESTRUCTIVE_PHRASE:
        _, rows = warehouse.execute_sql(
            "SELECT COUNT(*), MIN(file_date), MAX(file_date) FROM _file_ledger "
            f"WHERE dataset = '{params.dataset}' AND status = 'loaded'"
        )
        n, dmin, dmax = rows[0] if rows else (0, None, None)
        return make_error_response(
            code="CONFIRM_REQUIRED",
            message=(
                f"full=true would DELETE all local data for {params.dataset!r} "
                f"({n} loaded files spanning {dmin} → {dmax}) and re-download only "
                f"what Kiteworks still serves (~2 weeks retention). To proceed, "
                f"pass confirm_destructive='{CONFIRM_DESTRUCTIVE_PHRASE}'. "
                f"If you only need to recover or re-load files, prefer "
                f"bpd_reingest_local — it loads from the local raw archive "
                f"without deleting anything."
            ),
            fmt=params.response_format,
        )
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
    except RefreshWouldLoseHistory as e:
        return make_error_response(
            code="REFRESH_WOULD_LOSE_HISTORY",
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
