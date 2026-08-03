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


# --- Dimensional snapshot ordering (Patch #10) ------------------------------------------
#
# Dimensional datasets (DATASET_KINDS == 'dimensional': location_attr, item_attr,
# item_attr_extended) are full-universe keyed-overwrite snapshots: whichever file
# loads LAST wins outright, with no row-count change to signal anything. Loading a
# file older than the newest VERIFIED snapshot silently rolls the dimension back —
# the July 2026 location_attr regression, where a reingested 05-02 zip erased 12
# weeks of remodel dates.
#
# Staleness is only ever judged against VERIFIED state (adversarial-review fix):
#   - a file older than the ledger's newest status='loaded' file_date is stale;
#   - within a batch, candidates are tried NEWEST-FIRST and older ones are marked
#     stale only after a newer one actually loads. If the newest candidate is
#     corrupt or fails to download, the next-newest is attempted — a broken file
#     can never pin the dimension to an old snapshot or leave it empty.
# Bonus: at most one file per dimensional dataset loads per batch.


def _is_dimensional(dataset: str) -> bool:
    from .column_roles import DATASET_KINDS

    return DATASET_KINDS.get(dataset) == "dimensional"


def _ledger_loaded_max_file_date(warehouse: Warehouse, dataset: str) -> Any:
    """Newest file_date with a status='loaded' ledger row — VERIFIED state."""
    _, rows = warehouse.execute_sql(
        "SELECT MAX(file_date) FROM _file_ledger "
        f"WHERE dataset = '{dataset}' AND status = 'loaded'"
    )
    return rows[0][0] if rows and rows[0] else None


def _stale_dimension_note(file_date: Any, newest: Any) -> str:
    return (
        f"stale dimensional snapshot ({file_date} < newest {newest}); skipped — "
        "loading an older full-universe file would silently roll the dimension back"
    )


# --- Restatement detection (Patch #14) --------------------------------------------------
#
# Target RESTATES trailing weeks by re-uploading a file under the SAME
# Kiteworks file_id, bumping only `size` and `modified` (observed live
# 2026-08-03: three consecutive weekly sales files re-posted). The original
# skip predicate keyed on file_id + listing fingerprint — but live folder
# listings don't carry a usable fingerprint, so every restatement was
# indistinguishable from "already loaded" and never re-downloaded, leaving
# period_replace semantics dead weight.


def _parse_remote_modified(entry: dict[str, Any]) -> datetime | None:
    """Kiteworks `modified`/`clientModified` → naive-UTC datetime (best effort)."""
    raw = entry.get("modified") or entry.get("clientModified")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _remote_unchanged(prior: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Is the remote file provably the same content we already loaded?

    Precedence:
      1. Fingerprints on BOTH sides (content hash): equality decides outright.
      2. Remote `modified` newer than our download time → re-posted → reload.
    A reload is idempotent (per-key / period-replace upserts) and refreshes
    downloaded_at, so a restated file re-downloads exactly once.

    The listing's `size` is deliberately NOT compared against the ledger's
    `bytes`: bytes is the count we actually downloaded, and if Kiteworks'
    reported size ever differs from stored bytes (encryption padding, logical
    sizes), a size comparison would re-download every file on every sync.
    """
    fingerprint = entry.get("fingerprint")
    if fingerprint and prior.get("fingerprint"):
        return prior["fingerprint"] == fingerprint

    modified = _parse_remote_modified(entry)
    downloaded_at = prior.get("downloaded_at")
    if modified is not None and downloaded_at is not None:
        if isinstance(downloaded_at, datetime) and downloaded_at.tzinfo is not None:
            downloaded_at = downloaded_at.astimezone(UTC).replace(tzinfo=None)
        if isinstance(downloaded_at, datetime) and modified > downloaded_at:
            return False

    return True


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
    downloaded_at: datetime | None = None,
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
    # Broad on purpose (Patch #10 review fix): a truncated download raises
    # zipfile.BadZipFile (not ParseError), and one unreadable file must fail
    # its own ledger row, not crash the whole sync/reingest batch.
    except Exception as e:
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
                "downloaded_at": downloaded_at or datetime.now(UTC),
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
                "downloaded_at": downloaded_at or datetime.now(UTC),
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
            "downloaded_at": downloaded_at or datetime.now(UTC),
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
    if prior and prior.get("status") == "loaded" and _remote_unchanged(prior, entry):
        return FileOutcome(
            file_id=file_id,
            file_name=name,
            dataset=dataset,
            status="skipped",
        )
    if prior and prior.get("status") == "loaded":
        logger.info(
            "restated_file_detected",
            file_name=name,
            dataset=dataset,
            remote_size=entry.get("size"),
            ledger_bytes=prior.get("bytes"),
            remote_modified=str(entry.get("modified")),
            downloaded_at=str(prior.get("downloaded_at")),
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
            if prior and prior.get("status") == "loaded":
                # A failed RE-download of a restatement (Patch #14: this path
                # is newly reachable for loaded rows). The v1 rows are still
                # in the data table — do not demote the ledger row to
                # 'failed'/NULLs; leave it untouched so downloaded_at stays
                # older than the remote `modified` and the next sync retries.
                logger.warning(
                    "restated_redownload_failed_prior_load_preserved",
                    file_name=name,
                    dataset=dataset,
                    error=err_msg,
                )
                return FileOutcome(
                    file_id=file_id,
                    file_name=name,
                    dataset=dataset,
                    status="failed",
                    error=f"restated re-download failed (prior load preserved): {err_msg}",
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
    candidates: list[tuple[dict[str, Any], ParsedFilename]] = []
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
        # Patch #12: retired feeds are excluded from routine discovery — Target
        # sunset them, so a matching file reappearing in the folder is unusual
        # and should be a deliberate load (name the dataset in `datasets`).
        # Local reingest is unaffected: retired patterns stay classifiable.
        if parsed.pattern.retired and wanted is None:
            result.files_skipped += 1
            result.outcomes.append(
                FileOutcome(
                    file_id=str(entry.get("id", "")),
                    file_name=str(entry.get("name", "")),
                    dataset=parsed.pattern.dataset,
                    status="skipped",
                    error=(
                        "retired feed (Target sunset this file family); pass "
                        f"datasets=[\"{parsed.pattern.dataset}\"] to load it anyway"
                    ),
                )
            )
            continue
        candidates.append((entry, parsed))

    # Patch #10 stale-dimension guard: split candidates into regular files
    # (processed concurrently, as before) and per-dataset DIMENSIONAL groups
    # (newest-first with fallback — see the guard comment above). Files older
    # than the ledger's VERIFIED loaded max skip before download.
    targets: list[dict[str, Any]] = []
    dim_groups: dict[str, list[tuple[dict[str, Any], ParsedFilename]]] = {}
    for entry, parsed in candidates:
        ds = parsed.pattern.dataset
        if _is_dimensional(ds):
            dim_groups.setdefault(ds, []).append((entry, parsed))
        else:
            targets.append(entry)

    def _skip_stale(file_id: str, name: str, ds: str, file_date: Any, newest: Any) -> None:
        result.files_skipped += 1
        logger.info(
            "stale_dimensional_snapshot_skipped",
            file_name=name,
            dataset=ds,
            file_date=str(file_date),
            newest=str(newest),
        )
        result.outcomes.append(
            FileOutcome(
                file_id=file_id,
                file_name=name,
                dataset=ds,
                status="skipped",
                error=_stale_dimension_note(file_date, newest),
            )
        )

    for ds, group in dim_groups.items():
        group.sort(key=lambda t: (t[1].file_date, str(t[0].get("name", ""))), reverse=True)
        ledger_max = _ledger_loaded_max_file_date(warehouse, ds)
        if ledger_max is not None:
            kept = []
            for entry, parsed in group:
                if parsed.file_date < ledger_max:
                    _skip_stale(
                        str(entry.get("id", "")), str(entry.get("name", "")),
                        ds, parsed.file_date, ledger_max,
                    )
                else:
                    kept.append((entry, parsed))
            dim_groups[ds] = kept

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
        n_dry = len(targets)
        for ds, group in dim_groups.items():
            for i, (entry, _parsed) in enumerate(group):
                n_dry += 1
                result.outcomes.append(
                    FileOutcome(
                        file_id=str(entry["id"]),
                        file_name=str(entry["name"]),
                        dataset=ds,
                        status="dry_run",
                        error=None if i == 0 else (
                            "fallback candidate — loads only if every newer "
                            f"{ds} file fails"
                        ),
                    )
                )
        result.finished_at = datetime.now(UTC)
        result.notes = f"dry_run: would process {n_dry} file(s)"
        return result

    sem = asyncio.Semaphore(max(1, settings.bpd_max_parallel_downloads))

    async def _process_dimensional_group(
        ds: str, group: list[tuple[dict[str, Any], ParsedFilename]]
    ) -> list[FileOutcome]:
        """Newest-first; the first file that verifiably lands (loaded, or
        already-loaded per ledger fingerprint) makes every older one stale.
        A failing newest falls back to the next-newest instead of blocking it."""
        outcomes: list[FileOutcome] = []
        satisfied_date: Any = None
        for entry, parsed in group:
            if satisfied_date is not None:
                logger.info(
                    "stale_dimensional_snapshot_skipped",
                    file_name=str(entry.get("name", "")),
                    dataset=ds,
                    file_date=str(parsed.file_date),
                    newest=str(satisfied_date),
                )
                outcomes.append(
                    FileOutcome(
                        file_id=str(entry.get("id", "")),
                        file_name=str(entry.get("name", "")),
                        dataset=ds,
                        status="skipped",
                        error=_stale_dimension_note(parsed.file_date, satisfied_date),
                    )
                )
                continue
            outcome = await _process_one_file(client, warehouse, settings, entry, sem)
            outcomes.append(outcome)
            if outcome.status in ("loaded", "skipped"):
                satisfied_date = parsed.file_date
            else:
                logger.warning(
                    "dimensional_newest_failed_falling_back",
                    dataset=ds,
                    file_name=str(entry.get("name", "")),
                    error=outcome.error,
                )
        return outcomes

    coros: list[Any] = [
        _process_one_file(client, warehouse, settings, e, sem) for e in targets
    ]
    group_coros = [
        _process_dimensional_group(ds, group)
        for ds, group in dim_groups.items()
        if group
    ]
    gathered = await asyncio.gather(*coros, *group_coros, return_exceptions=False)
    outcomes = []
    for item in gathered:
        if isinstance(item, list):
            outcomes.extend(item)
        else:
            outcomes.append(item)
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
    _log_unresolvable_roles(warehouse)

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


def _log_unresolvable_roles(warehouse: Warehouse) -> None:
    """Post-load warning pass over REQUIRED_ROLES (Patch #10). Log-only —
    the hard gate is the roles_resolvable health check."""
    try:
        from .column_roles import validate_roles

        for failure in validate_roles(warehouse):
            logger.warning("role_unresolvable", **failure)
    except Exception as e:
        logger.warning("role_validation_failed", error=str(e))


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

    # Patch #10 stale-dimension guard (see the guard comment near the top of
    # this module): regular files load ascending; dimensional datasets are
    # grouped and tried NEWEST-FIRST — older candidates go stale only once a
    # newer one verifiably loads, and files older than the ledger's loaded max
    # skip outright.
    regular: list[tuple[Any, Path]] = []
    dim_groups: dict[str, list[tuple[Any, Path]]] = {}
    for parsed, zp in targets:
        ds = parsed.pattern.dataset
        if _is_dimensional(ds):
            dim_groups.setdefault(ds, []).append((parsed, zp))
        else:
            regular.append((parsed, zp))

    def _skip_stale_local(zp: Path, ds: str, file_date: Any, newest: Any) -> None:
        result.files_skipped += 1
        logger.info(
            "stale_dimensional_snapshot_skipped",
            file_name=zp.name,
            dataset=ds,
            file_date=str(file_date),
            newest=str(newest),
        )
        result.outcomes.append(
            FileOutcome(
                file_id=f"local:{zp.name}",
                file_name=zp.name,
                dataset=ds,
                status="skipped",
                error=_stale_dimension_note(file_date, newest),
            )
        )

    for ds, group in dim_groups.items():
        group.sort(key=lambda t: (t[0].file_date, t[1].name), reverse=True)
        ledger_max = _ledger_loaded_max_file_date(warehouse, ds)
        if ledger_max is not None:
            kept: list[tuple[Any, Path]] = []
            for parsed, zp in group:
                if parsed.file_date < ledger_max:
                    _skip_stale_local(zp, ds, parsed.file_date, ledger_max)
                else:
                    kept.append((parsed, zp))
            dim_groups[ds] = kept

    # Ascending file_date so period-replace datasets end with the newest
    # generation per period, mirroring what live syncs would have produced.
    regular.sort(key=lambda t: (t[0].file_date, t[1].name))
    n_targets = len(regular) + sum(len(g) for g in dim_groups.values())

    if dry_run:
        for parsed, zp in regular:
            result.outcomes.append(
                FileOutcome(
                    file_id=f"local:{zp.name}",
                    file_name=zp.name,
                    dataset=parsed.pattern.dataset,
                    status="dry_run",
                )
            )
        for ds, group in dim_groups.items():
            for i, (_parsed, zp) in enumerate(group):
                result.outcomes.append(
                    FileOutcome(
                        file_id=f"local:{zp.name}",
                        file_name=zp.name,
                        dataset=ds,
                        status="dry_run",
                        error=None if i == 0 else (
                            "fallback candidate — loads only if every newer "
                            f"{ds} file fails"
                        ),
                    )
                )
        result.finished_at = datetime.now(UTC)
        result.notes = f"dry_run: would reingest {n_targets} file(s) from raw_dir"
        return result

    if n_targets and not warehouse.read_only:
        try:
            create_backup(warehouse, settings, reason="pre-reingest")
        except Exception as e:
            logger.warning("auto_backup_failed", error=str(e))

    # Patch #13: re-processing a file that already has a ledger row must
    # UPDATE that row, not append a 'local:' shadow under a different file_id
    # (the ledger PK) — shadows inflated per-dataset file counts by one per
    # re-processed file. Prefer the most recently loaded non-local id. The
    # Kiteworks fingerprint is reused ONLY from a status='loaded' row: a
    # failed-download row stores the REMOTE fingerprint of bytes that never
    # reached disk, and stamping it onto a reingest of the older on-disk zip
    # would make live sync skip the real file forever (review fix: major).
    _, ledger_rows = warehouse.execute_sql(
        "SELECT file_name, file_id, fingerprint, status, downloaded_at "
        "FROM _file_ledger "
        "ORDER BY (file_id LIKE 'local:%'), loaded_at DESC NULLS LAST"
    )
    known_rows: dict[str, tuple[str, Any, Any]] = {}
    for fname, fid, fp, status, dl_at in ledger_rows:
        known_rows.setdefault(
            fname, (fid, fp if status == "loaded" else None, dl_at)
        )

    async def _load_local(parsed: Any, zp: Path) -> FileOutcome:
        # Preserve the original downloaded_at when reusing a row: a reingest
        # re-parses LOCAL bytes, and downloaded_at must keep meaning "when we
        # fetched the remote bytes" — bumping it here would mask any remote
        # restatement posted since (review fix: the next sync's
        # modified-vs-downloaded_at test would wrongly skip it forever).
        file_id, fingerprint, prior_downloaded_at = known_rows.get(
            zp.name, (f"local:{zp.name}", None, None)
        )
        outcome = await _parse_and_load_zip(
            warehouse,
            parsed=parsed,
            zip_path=zp,
            file_id=file_id,
            name=zp.name,
            folder_id="local",
            fingerprint=fingerprint,
            bytes_written=zp.stat().st_size,
            downloaded_at=prior_downloaded_at,
        )
        result.outcomes.append(outcome)
        if outcome.status == "loaded":
            result.files_loaded += 1
        elif outcome.status == "failed":
            result.files_failed += 1
        return outcome

    for parsed, zp in regular:
        await _load_local(parsed, zp)
    for ds, group in dim_groups.items():
        satisfied_date: Any = None
        for parsed, zp in group:
            if satisfied_date is not None:
                _skip_stale_local(zp, ds, parsed.file_date, satisfied_date)
                continue
            outcome = await _load_local(parsed, zp)
            if outcome.status == "loaded":
                satisfied_date = parsed.file_date
            else:
                logger.warning(
                    "dimensional_newest_failed_falling_back",
                    dataset=ds,
                    file_name=zp.name,
                    error=outcome.error,
                )
    result.files_new = result.files_loaded + result.files_failed

    try:
        warehouse.ensure_views()
    except Exception as e:
        logger.warning("ensure_views_failed", error=str(e))
    _log_unresolvable_roles(warehouse)

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
