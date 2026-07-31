"""Sync worker: discover new BPD files in Kiteworks, download, parse, load."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import KiteworksAPIError, KiteworksClient
from .config import Settings
from .logging_setup import get_logger
from .parsers import (
    Dataset,
    ParsedFilename,
    ParseError,
    classify_filename,
    derive_duckdb_schema,
    read_dataframe,
)
from .warehouse import Warehouse, _pattern_for

logger = get_logger(__name__)


@dataclass
class FileOutcome:
    file_id: str
    file_name: str
    dataset: str | None
    status: str  # 'loaded' | 'skipped' | 'failed' | 'unknown_pattern'
    rows: int = 0
    bytes: int = 0
    error: str | None = None


@dataclass
class SyncResult:
    started_at: datetime
    finished_at: datetime
    triggered_by: str
    folder_id: str | None
    files_found: int = 0
    files_new: int = 0
    files_loaded: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    files_unknown: int = 0
    outcomes: list[FileOutcome] = field(default_factory=list)
    notes: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_s": (self.finished_at - self.started_at).total_seconds(),
            "triggered_by": self.triggered_by,
            "folder_id": self.folder_id,
            "files_found": self.files_found,
            "files_new": self.files_new,
            "files_loaded": self.files_loaded,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "files_unknown": self.files_unknown,
            "notes": self.notes,
        }


async def _find_vendor_folder(
    client: KiteworksClient, vendor_id: str
) -> dict[str, Any] | None:
    """Locate the top-level folder whose name == vendor_id.

    The location_attr folder (`ALL_WKLY_LOC_ATTR_...` zips) lives somewhere reachable
    from the top folders; we recurse into immediate children if not at the top level.
    """
    tops = await client.list_top_folders()
    # Exact match first.
    for f in tops:
        if str(f.get("name", "")).strip() == str(vendor_id):
            return f
    # Loose match (case-insensitive contains).
    for f in tops:
        if str(vendor_id) in str(f.get("name", "")):
            return f
    return None


async def _iter_files_recursive(
    client: KiteworksClient, folder_id: str, *, depth: int = 0, max_depth: int = 3
) -> list[dict[str, Any]]:
    """Walk a folder tree and return only file entries. Depth-capped to be safe."""
    children = await client.list_folder_children(folder_id)
    files: list[dict[str, Any]] = []
    subfolders: list[str] = []
    for c in children:
        t = str(c.get("type", "")).lower()
        if t == "f":  # Kiteworks uses 'f' for file (Folder.type also says "f - file")
            files.append(c)
        elif t == "d":
            subfolders.append(str(c.get("id")))
    if depth < max_depth:
        for sub_id in subfolders:
            files.extend(await _iter_files_recursive(client, sub_id, depth=depth + 1, max_depth=max_depth))
    return files


def _enforce_raw_dir_cap(
    raw_dir: Path, max_bytes: int, *, loaded_names: set[str] | None = None
) -> None:
    """LRU-evict oldest zips when raw_dir exceeds max_bytes.

    Patch #9: zips whose file_name has no status='loaded' ledger row are NEVER
    evicted — with ~2-week Kiteworks retention, an un-ingested zip on disk may
    be the only copy of that data anywhere. They are skipped with a warning
    even if that leaves the dir over the cap.
    """
    if not raw_dir.exists():
        return
    zips = sorted(
        (p for p in raw_dir.glob("*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in zips)
    while total > max_bytes and zips:
        victim = zips.pop(0)
        if loaded_names is not None and victim.name not in loaded_names:
            logger.warning(
                "raw_dir_evict_skipped_unledgered",
                path=str(victim),
                reason="zip not loaded into the warehouse; may be the only copy "
                "(run bpd_reingest_local to ingest it, after which it becomes evictable)",
            )
            continue
        try:
            sz = victim.stat().st_size
            victim.unlink()
            total -= sz
            logger.info("raw_dir_evicted", path=str(victim), bytes=sz)
        except OSError as e:
            logger.warning("raw_dir_evict_failed", path=str(victim), error=str(e))
            break


def _pick_primary_key(
    parsed: ParsedFilename, df_columns: Iterable[str]
) -> tuple[str, ...]:
    """First candidate from the catalog whose columns all exist in the df."""
    cols = set(df_columns)
    for candidate in parsed.pattern.primary_key_candidates:
        if all(c in cols for c in candidate):
            return candidate
    # Fall back to the first candidate (warehouse will log a warning if it's missing).
    return parsed.pattern.primary_key_candidates[0]


# --- Warehouse backups (Patch #9) ------------------------------------------------------

def create_backup(
    warehouse: Warehouse, settings: Settings, *, reason: str, force: bool = False
) -> Path | None:
    """Timestamped consistent copy of the warehouse into settings.backups_dir.

    Retention: keeps the newest `settings.bpd_backup_keep` backups, pruning
    older ones. Returns the backup path (None when backups are disabled).
    `force=True` backs up even when bpd_auto_backup is off — used before
    destructive deletes, where skipping the snapshot is never acceptable.
    """
    if not (settings.bpd_auto_backup or force):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = settings.backups_dir / f"bpd-{stamp}-{reason}.duckdb"
    warehouse.backup_to(dest)
    backups = sorted(
        settings.backups_dir.glob("bpd-*.duckdb"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[max(1, settings.bpd_backup_keep):]:
        try:
            stale.unlink()
            logger.info("backup_pruned", path=str(stale))
        except OSError as e:
            logger.warning("backup_prune_failed", path=str(stale), error=str(e))
    return dest


# --- Destructive-refresh guardrails (Patch #9) -----------------------------------------

# The exact phrase bpd_refresh_dataset(full=true) requires. A bare boolean was
# too easy to pass casually; with ~2-week Kiteworks retention, a full refresh
# permanently destroys any local history older than what Target still serves.
CONFIRM_DESTRUCTIVE_PHRASE = "I understand this deletes local history"


class RefreshWouldLoseHistory(RuntimeError):
    """Raised when full=true would delete local history Kiteworks can no longer re-serve."""




async def _parse_and_load_zip(
    warehouse: Warehouse,
    *,
    parsed: ParsedFilename,
    zip_path: Path,
    file_id: str,
    name: str,
    folder_id: str,
    fingerprint: Any,
    bytes_written: int,
) -> FileOutcome:
    """Parse a local zip and load it into the warehouse, writing ledger rows.

    Shared by the network sync path (`_process_one_file`, after download) and
    the local reingest path (`reingest_local_files`, Patch #9 — recovers data
    from zips already on disk without touching Kiteworks).
    """
    dataset: Dataset = parsed.pattern.dataset
    loop = asyncio.get_running_loop()
    try:
        parse_result = await loop.run_in_executor(
            None, read_dataframe, zip_path, dataset
        )
    except ParseError as e:
        err_msg = f"{type(e).__name__}: {e}"
        logger.warning(
            "file_parse_failed",
            file_name=name,
            dataset=dataset,
            error=err_msg,
        )
        warehouse.ledger_upsert(
            {
                "file_id": file_id,
                "file_name": name,
                "folder_id": folder_id,
                "dataset": dataset,
                "file_date": parsed.file_date,
                "bytes": bytes_written,
                "fingerprint": fingerprint,
                "downloaded_at": datetime.now(UTC),
                "loaded_at": None,
                "row_count": None,
                "status": "failed",
                "error_message": err_msg,
                "parse_method": "failed",
            }
        )
        return FileOutcome(
            file_id=file_id,
            file_name=name,
            dataset=dataset,
            status="failed",
            bytes=bytes_written,
            error=f"parse: {e}",
        )

    df = parse_result.df
    if parse_result.method != "strict":
        logger.warning(
            "file_parse_used_fallback",
            file_name=name,
            dataset=dataset,
            method=parse_result.method,
            skipped_rows=parse_result.skipped_rows,
            primary_error=parse_result.primary_error,
        )

    # Canonical column-name renames are applied INSIDE read_dataframe
    # (before type-hint casts — see parsers._finalize, Patch #8), so `df`
    # already carries canonical names here.

    columns = derive_duckdb_schema(df)
    primary_key = _pick_primary_key(parsed, df.columns)

    warehouse.ensure_data_table(dataset, columns)
    try:
        rows = warehouse.upsert_dataframe(
            dataset,
            df,
            primary_key=primary_key,
            replace_scope=parsed.pattern.replace_scope,
        )
    except Exception as e:
        err_msg = f"load: {type(e).__name__}: {e}"
        logger.warning(
            "file_load_failed", file_name=name, dataset=dataset, error=err_msg
        )
        warehouse.ledger_upsert(
            {
                "file_id": file_id,
                "file_name": name,
                "folder_id": folder_id,
                "dataset": dataset,
                "file_date": parsed.file_date,
                "bytes": bytes_written,
                "fingerprint": fingerprint,
                "downloaded_at": datetime.now(UTC),
                "loaded_at": None,
                "row_count": None,
                "status": "failed",
                "error_message": err_msg,
                "parse_method": parse_result.method,
            }
        )
        return FileOutcome(
            file_id=file_id,
            file_name=name,
            dataset=dataset,
            status="failed",
            bytes=bytes_written,
            error=err_msg,
        )

    # Register schema only after a successful load, so a failed upsert
    # doesn't leave the registry pointing at types we didn't actually
    # persist (Patch #6).
    prior_schema = warehouse.register_schema(dataset, columns, primary_key)
    if prior_schema:
        prior_cols = set(prior_schema)
        new_cols = set(columns)
        added = sorted(new_cols - prior_cols)
        removed = sorted(prior_cols - new_cols)
        logger.warning(
            "schema_drift",
            dataset=dataset,
            added=added,
            removed=removed,
        )

    # Successful load. If a fallback path was used, record the diagnostic message
    # alongside the loaded row so users can see *which* files needed permissive
    # parsing without trawling the logs.
    loaded_error_msg = None
    if parse_result.method != "strict":
        loaded_error_msg = (
            f"loaded via fallback method={parse_result.method}; "
            f"skipped {parse_result.skipped_rows} rows; "
            f"primary error: {parse_result.primary_error}"
        )

    warehouse.ledger_upsert(
        {
            "file_id": file_id,
            "file_name": name,
            "folder_id": folder_id,
            "dataset": dataset,
            "file_date": parsed.file_date,
            "bytes": bytes_written,
            "fingerprint": fingerprint,
            "downloaded_at": datetime.now(UTC),
            "loaded_at": datetime.now(UTC),
            "row_count": rows,
            "status": "loaded",
            "error_message": loaded_error_msg,
            "parse_method": parse_result.method,
        }
    )

    return FileOutcome(
        file_id=file_id,
        file_name=name,
        dataset=dataset,
        status="loaded",
        rows=rows,
        bytes=bytes_written,
    )


async def _process_one_file(
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    entry: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> FileOutcome:
    file_id = str(entry["id"])
    name = str(entry["name"])
    folder_id = str(entry.get("parentId") or "")
    fingerprint = entry.get("fingerprint")

    parsed = classify_filename(name)
    if parsed is None:
        logger.debug("unknown_file_pattern", file=name, file_id=file_id)
        return FileOutcome(
            file_id=file_id,
            file_name=name,
            dataset=None,
            status="unknown_pattern",
        )

    dataset: Dataset = parsed.pattern.dataset
    prior = warehouse.ledger_seen(file_id)
    if (
        prior
        and prior.get("status") == "loaded"
        and (not fingerprint or prior.get("fingerprint") == fingerprint)
    ):
        return FileOutcome(
            file_id=file_id,
            file_name=name,
            dataset=dataset,
            status="skipped",
        )

    async with semaphore:
        zip_path = settings.raw_dir / name
        try:
            bytes_written = await client.download_file(file_id, zip_path)
        except KiteworksAPIError as e:
            err_msg = f"download: HTTP {e.status}: {e.body or e}"
            logger.warning(
                "file_download_failed", file_name=name, dataset=dataset, error=err_msg
            )
            warehouse.ledger_upsert(
                {
                    "file_id": file_id,
                    "file_name": name,
                    "folder_id": folder_id,
                    "dataset": dataset,
                    "file_date": parsed.file_date,
                    "bytes": None,
                    "fingerprint": fingerprint,
                    "downloaded_at": datetime.now(UTC),
                    "loaded_at": None,
                    "row_count": None,
                    "status": "failed",
                    "error_message": err_msg,
                    "parse_method": None,
                }
            )
            return FileOutcome(
                file_id=file_id,
                file_name=name,
                dataset=dataset,
                status="failed",
                error=err_msg,
            )

        warehouse.ledger_upsert(
            {
                "file_id": file_id,
                "file_name": name,
                "folder_id": folder_id,
                "dataset": dataset,
                "file_date": parsed.file_date,
                "bytes": bytes_written,
                "fingerprint": fingerprint,
                "downloaded_at": datetime.now(UTC),
                "loaded_at": None,
                "row_count": None,
                "status": "downloaded",
                "error_message": None,
                "parse_method": None,
            }
        )

        return await _parse_and_load_zip(
            warehouse,
            parsed=parsed,
            zip_path=zip_path,
            file_id=file_id,
            name=name,
            folder_id=folder_id,
            fingerprint=fingerprint,
            bytes_written=bytes_written,
        )


async def sync_new_files(
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    *,
    datasets: Iterable[str] | None = None,
    triggered_by: str = "manual",
    dry_run: bool = False,
    auto_backup: bool = True,
) -> SyncResult:
    """Walk the vendor folder, download/parse/load any new BPD files. Idempotent."""
    started = datetime.now(UTC)
    result = SyncResult(
        started_at=started,
        finished_at=started,
        triggered_by=triggered_by,
        folder_id=None,
    )
    settings.ensure_dirs()

    # Patch #9: snapshot the warehouse before mutating it. Best-effort here —
    # a backup failure shouldn't block a routine additive sync (refresh_dataset
    # takes its own MANDATORY backup before deleting anything).
    if auto_backup and not dry_run and not warehouse.read_only:
        try:
            create_backup(warehouse, settings, reason="pre-sync")
        except Exception as e:
            logger.warning("auto_backup_failed", error=str(e))

    folder = await _find_vendor_folder(client, settings.bpd_vendor_id)
    if not folder:
        result.finished_at = datetime.now(UTC)
        result.notes = f"vendor folder {settings.bpd_vendor_id} not found in top folders"
        if not dry_run:
            warehouse.log_sync(
                started_at=started,
                finished_at=result.finished_at,
                triggered_by=triggered_by,
                files_new=0,
                files_loaded=0,
                files_failed=0,
                notes=result.notes,
            )
        return result

    result.folder_id = str(folder["id"])
    files = await _iter_files_recursive(client, result.folder_id)
    result.files_found = len(files)

    # Filter to known patterns and (optionally) the requested datasets.
    wanted = set(datasets) if datasets else None
    targets: list[dict[str, Any]] = []
    for entry in files:
        parsed = classify_filename(str(entry.get("name", "")))
        if parsed is None:
            result.files_unknown += 1
            result.outcomes.append(
                FileOutcome(
                    file_id=str(entry.get("id", "")),
                    file_name=str(entry.get("name", "")),
                    dataset=None,
                    status="unknown_pattern",
                )
            )
            continue
        if wanted is not None and parsed.pattern.dataset not in wanted:
            continue
        targets.append(entry)

    if dry_run:
        for entry in targets:
            parsed = classify_filename(str(entry["name"]))
            result.outcomes.append(
                FileOutcome(
                    file_id=str(entry["id"]),
                    file_name=str(entry["name"]),
                    dataset=parsed.pattern.dataset if parsed else None,
                    status="dry_run",
                )
            )
        result.finished_at = datetime.now(UTC)
        result.notes = f"dry_run: would process {len(targets)} file(s)"
        return result

    sem = asyncio.Semaphore(max(1, settings.bpd_max_parallel_downloads))
    coros = [_process_one_file(client, warehouse, settings, e, sem) for e in targets]
    outcomes = await asyncio.gather(*coros, return_exceptions=False)
    result.outcomes.extend(outcomes)

    for o in outcomes:
        if o.status == "loaded":
            result.files_loaded += 1
        elif o.status == "skipped":
            result.files_skipped += 1
        elif o.status == "failed":
            result.files_failed += 1
        elif o.status == "unknown_pattern":
            result.files_unknown += 1
    result.files_new = result.files_loaded + result.files_failed

    # Best-effort cleanup of any leftover extract directory and enforce raw cap.
    try:
        if settings.extract_dir.exists():
            for p in settings.extract_dir.iterdir():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("extract_cleanup_failed", error=str(e))

    loaded_names = {
        r[0]
        for r in warehouse.execute_sql(
            "SELECT file_name FROM _file_ledger WHERE status = 'loaded'"
        )[1]
    }
    _enforce_raw_dir_cap(
        settings.raw_dir, settings.bpd_raw_dir_max_bytes, loaded_names=loaded_names
    )

    # Refresh views after loads.
    try:
        warehouse.ensure_views()
    except Exception as e:
        logger.warning("ensure_views_failed", error=str(e))

    result.finished_at = datetime.now(UTC)
    warehouse.log_sync(
        started_at=started,
        finished_at=result.finished_at,
        triggered_by=triggered_by,
        files_new=result.files_new,
        files_loaded=result.files_loaded,
        files_failed=result.files_failed,
        notes=result.notes,
    )
    return result


async def _remote_min_file_date(
    client: KiteworksClient, settings: Settings, dataset: str
):
    """Oldest file_date Kiteworks currently serves for `dataset` (None if none)."""
    folder = await _find_vendor_folder(client, settings.bpd_vendor_id)
    if not folder:
        return None
    files = await _iter_files_recursive(client, str(folder["id"]))
    dates = []
    for entry in files:
        parsed = classify_filename(str(entry.get("name", "")))
        if parsed is not None and parsed.pattern.dataset == dataset:
            dates.append(parsed.file_date)
    return min(dates) if dates else None


async def refresh_dataset(
    client: KiteworksClient,
    warehouse: Warehouse,
    settings: Settings,
    *,
    dataset: str,
    full: bool = False,
    triggered_by: str = "refresh_dataset",
) -> SyncResult:
    """Re-load a single dataset. If `full=True`, clear the existing table and ledger first.

    Patch #9 guardrails for `full=True` (Kiteworks retains only ~2 weeks, so a
    full refresh permanently destroys any older local history):
    1. HARD REFUSAL when the dataset's local history extends earlier than the
       oldest file Kiteworks can re-serve — raises RefreshWouldLoseHistory.
       Recover missing files from disk with `bpd_reingest_local` or restore a
       backup instead. (The confirm-phrase gate lives in the tool layer.)
    2. MANDATORY backup before any deletion; a backup failure aborts the refresh.
    """
    # Validate the dataset name against the catalog.
    _ = _pattern_for(dataset)

    if full and not warehouse.read_only:
        _, rows = warehouse.execute_sql(
            "SELECT MIN(file_date), COUNT(*) FROM _file_ledger "
            f"WHERE dataset = '{dataset}' AND status = 'loaded'"
        )
        local_min, local_files = rows[0] if rows else (None, 0)
        if local_min is not None:
            remote_min = await _remote_min_file_date(client, settings, dataset)
            if remote_min is None or local_min < remote_min:
                raise RefreshWouldLoseHistory(
                    f"full=true refused for {dataset!r}: local history starts "
                    f"{local_min} ({local_files} loaded files) but the oldest file "
                    f"Kiteworks can re-serve is {remote_min or 'NONE'} — the refresh "
                    f"would permanently destroy everything older. If specific files "
                    f"need re-loading, use bpd_reingest_local (loads from the local "
                    f"raw archive without deleting anything) or restore a backup "
                    f"from the backups directory."
                )

        # Snapshot before deleting anything. This backup is mandatory: if it
        # fails, the refresh must not proceed. force=True so even
        # bpd_auto_backup=false cannot skip a pre-delete snapshot.
        create_backup(warehouse, settings, reason=f"pre-refresh-{dataset}", force=True)

        from .warehouse import quote_ident

        with warehouse._lock:  # type: ignore[attr-defined]
            warehouse._conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM _file_ledger WHERE dataset = ?", [dataset]
            )
            # If the table exists, truncate it.
            exists = warehouse._conn.execute(  # type: ignore[attr-defined]
                "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
                [dataset],
            ).fetchone()
            if exists:
                warehouse._conn.execute(f"DELETE FROM {quote_ident(dataset)}")  # type: ignore[attr-defined]

    return await sync_new_files(
        client,
        warehouse,
        settings,
        datasets={dataset},
        triggered_by=triggered_by,
        auto_backup=not full,  # full path already took its mandatory backup
    )


async def reingest_local_files(
    warehouse: Warehouse,
    settings: Settings,
    *,
    datasets: Iterable[str] | None = None,
    only_unledgered: bool = True,
    dry_run: bool = False,
    triggered_by: str = "bpd_reingest_local",
) -> SyncResult:
    """Load BPD zips already on disk (settings.raw_dir) WITHOUT touching Kiteworks.

    Patch #9 recovery path: after a destructive refresh, the raw zip archive on
    disk is often the only surviving copy of history Kiteworks no longer serves
    (~2-week retention). This walks raw_dir, classifies each zip by filename,
    and runs it through the exact same parse→rename→upsert pipeline as a
    network sync — so replace-scope semantics, ledger rows, and idempotency all
    behave identically.

    - `only_unledgered=True` (default) skips zips whose file_name already has a
      status='loaded' ledger row — re-running is cheap and idempotent.
    - Files load in ascending file_date order (name as tie-break), so for
      period-replace datasets later generations land last, matching live-sync
      outcomes.
    - Ledger rows for reingested files use file_id 'local:<file_name>' (the
      original Kiteworks file_id is unknowable offline).
    """
    started = datetime.now(UTC)
    result = SyncResult(
        started_at=started,
        finished_at=started,
        triggered_by=triggered_by,
        folder_id="local",
    )
    settings.ensure_dirs()

    wanted = set(datasets) if datasets else None
    _, loaded_rows = warehouse.execute_sql(
        "SELECT file_name FROM _file_ledger WHERE status = 'loaded'"
    )
    loaded_names = {r[0] for r in loaded_rows}

    targets: list[tuple[Any, Path]] = []
    for zp in sorted(settings.raw_dir.glob("*.zip")):
        result.files_found += 1
        parsed = classify_filename(zp.name)
        if parsed is None:
            result.files_unknown += 1
            result.outcomes.append(
                FileOutcome(
                    file_id=f"local:{zp.name}",
                    file_name=zp.name,
                    dataset=None,
                    status="unknown_pattern",
                )
            )
            continue
        if wanted is not None and parsed.pattern.dataset not in wanted:
            continue
        if only_unledgered and zp.name in loaded_names:
            result.files_skipped += 1
            result.outcomes.append(
                FileOutcome(
                    file_id=f"local:{zp.name}",
                    file_name=zp.name,
                    dataset=parsed.pattern.dataset,
                    status="skipped",
                )
            )
            continue
        targets.append((parsed, zp))

    # Ascending file_date so period-replace datasets end with the newest
    # generation per period, mirroring what live syncs would have produced.
    targets.sort(key=lambda t: (t[0].file_date, t[1].name))

    if dry_run:
        for parsed, zp in targets:
            result.outcomes.append(
                FileOutcome(
                    file_id=f"local:{zp.name}",
                    file_name=zp.name,
                    dataset=parsed.pattern.dataset,
                    status="dry_run",
                )
            )
        result.finished_at = datetime.now(UTC)
        result.notes = f"dry_run: would reingest {len(targets)} file(s) from raw_dir"
        return result

    if targets and not warehouse.read_only:
        try:
            create_backup(warehouse, settings, reason="pre-reingest")
        except Exception as e:
            logger.warning("auto_backup_failed", error=str(e))

    for parsed, zp in targets:
        outcome = await _parse_and_load_zip(
            warehouse,
            parsed=parsed,
            zip_path=zp,
            file_id=f"local:{zp.name}",
            name=zp.name,
            folder_id="local",
            fingerprint=None,
            bytes_written=zp.stat().st_size,
        )
        result.outcomes.append(outcome)
        if outcome.status == "loaded":
            result.files_loaded += 1
        elif outcome.status == "failed":
            result.files_failed += 1
    result.files_new = result.files_loaded + result.files_failed

    try:
        warehouse.ensure_views()
    except Exception as e:
        logger.warning("ensure_views_failed", error=str(e))

    result.finished_at = datetime.now(UTC)
    result.notes = "local reingest from raw_dir (no downloads)"
    warehouse.log_sync(
        started_at=started,
        finished_at=result.finished_at,
        triggered_by=triggered_by,
        files_new=result.files_new,
        files_loaded=result.files_loaded,
        files_failed=result.files_failed,
        notes=result.notes,
    )
    return result
