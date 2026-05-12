"""Admin tools: auth_status, cache_status, clear_cache."""

from __future__ import annotations

import shutil
from typing import Any

from ..auth import AuthManager
from ..client import KiteworksAPIError, KiteworksClient
from ..config import Settings
from ..formatting import make_error_response, make_kv_response
from ..schemas import (
    AuthStatusInput,
    CacheStatusInput,
    ClearCacheInput,
    ToolResponse,
)
from ..warehouse import Warehouse


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
    info = warehouse.disk_stats()
    raw_bytes = _dir_bytes(settings.raw_dir)
    db_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    dataset_rows = warehouse.list_datasets()
    overall_min = min(
        (r["min_date"] for r in dataset_rows if r["min_date"] is not None),
        default=None,
    )
    overall_max = max(
        (r["max_date"] for r in dataset_rows if r["max_date"] is not None),
        default=None,
    )
    payload = {
        "data_dir": str(settings.data_dir),
        "raw_dir_bytes": raw_bytes,
        "duckdb_file_bytes": db_bytes,
        "ledger_files": info["ledger_file_count"],
        "ledger_total_bytes": info["ledger_bytes_total"],
        "datasets_loaded": len(dataset_rows),
        "earliest_data_date": overall_min,
        "latest_data_date": overall_max,
        "last_sync_finished_at": info["last_sync_finished_at"],
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
