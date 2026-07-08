"""DuckDB warehouse helpers: bootstrap schema, register schemas, idempotent loads.

Patch #3 design: a single physical `bpd.duckdb` file owned by one writable
connection. Engine-level read-only execution for `bpd_run_sql` is provided by the
`ReadOnlyView` facade further down: each query runs inside a fresh cursor wrapped
in `BEGIN TRANSACTION READ ONLY` / `ROLLBACK`. DuckDB rejects writes inside such a
transaction at the engine layer (verified on 1.5.2).

Earlier patches forked the file into a `.duckdb.ro` snapshot to work around
DuckDB's same-file-mixed-mode restriction. That approach caused schema drift
(migrations on the writable copy weren't reflected in the snapshot). The
`.ro` mechanism has been removed; `cleanup_legacy_snapshot` deletes any leftover
file from prior installs on startup.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from .logging_setup import get_logger
from .parsers import PATTERNS, Dataset, FilePattern

logger = get_logger(__name__)


METADATA_DDL = """
CREATE TABLE IF NOT EXISTS _file_ledger (
    file_id        TEXT PRIMARY KEY,
    file_name      TEXT NOT NULL,
    folder_id      TEXT,
    dataset        TEXT NOT NULL,
    file_date      DATE,
    bytes          BIGINT,
    fingerprint    TEXT,
    downloaded_at  TIMESTAMP NOT NULL,
    loaded_at      TIMESTAMP,
    row_count      BIGINT,
    status         TEXT NOT NULL,
    error_message  TEXT,
    parse_method   TEXT
);

CREATE TABLE IF NOT EXISTS _sync_log (
    sync_id       UUID DEFAULT uuid(),
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    triggered_by  TEXT,
    files_new     INTEGER,
    files_loaded  INTEGER,
    files_failed  INTEGER,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS _schema_registry (
    dataset      TEXT PRIMARY KEY,
    column_json  TEXT NOT NULL,
    first_seen   TIMESTAMP NOT NULL,
    last_seen    TIMESTAMP NOT NULL,
    primary_key  TEXT
);
"""

# Migrations: ALTER ADD COLUMN IF NOT EXISTS so existing warehouses pick up new
# columns without losing data. Each entry must be idempotent on its own.
_MIGRATIONS = (
    "ALTER TABLE _file_ledger ADD COLUMN IF NOT EXISTS error_message TEXT",
    "ALTER TABLE _file_ledger ADD COLUMN IF NOT EXISTS parse_method TEXT",
)


def _pattern_for(dataset: str) -> FilePattern:
    for p in PATTERNS:
        if p.dataset == dataset:
            return p
    raise KeyError(f"unknown dataset {dataset!r}")


def quote_ident(name: str) -> str:
    """DuckDB identifier quoting — only safe values are A-Z, a-z, 0-9, underscore."""
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    return f'"{safe}"'


def _coerce_date(v: Any) -> Any:
    """Best-effort: if `v` is a string that looks like an ISO date, parse to a date.

    Returns the original value if it doesn't look like a date (e.g. it's already
    a date/datetime, or it's NULL, or it's a non-date string we can't interpret).
    """
    from datetime import date as _date
    from datetime import datetime as _dt

    if v is None or isinstance(v, _date | _dt):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
            try:
                return _dt.strptime(v[:10], fmt).date()
            except ValueError:
                continue
    return v


class Warehouse:
    """Single-process wrapper around a DuckDB connection plus a thread lock.

    Pass `read_only=True` to open an engine-level read-only connection on a file
    *no other connection in this process has open*. In production we keep ONE
    writable Warehouse per process and use the `ReadOnlyView` facade for
    `bpd_run_sql` (engine-level RO via transaction). The `read_only=True` path
    is retained for tests that want to verify DuckDB's own write-rejection
    behavior on a connection it considers read-only.
    """

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(db_path), read_only=read_only)
        # DuckDB connection objects are not thread-safe across operations; serialize.
        # RLock so helper methods (e.g. detect_date_column) can be called while a
        # caller already holds the lock without deadlocking.
        self._lock = threading.RLock()
        if not read_only:
            with self._lock:
                self._conn.execute(METADATA_DDL)
                for stmt in _MIGRATIONS:
                    self._conn.execute(stmt)

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- schema ----------

    def register_schema(
        self, dataset: Dataset, columns: dict[str, str], primary_key: tuple[str, ...]
    ) -> dict[str, str] | None:
        """Insert/update _schema_registry. Returns the prior column map if it existed
        and differs from the incoming one (schema drift)."""
        if self._read_only:
            raise RuntimeError("read-only warehouse cannot register schema")
        now = datetime.now(UTC)
        with self._lock:
            row = self._conn.execute(
                "SELECT column_json FROM _schema_registry WHERE dataset = ?",
                [dataset],
            ).fetchone()
            prior = json.loads(row[0]) if row else None
            self._conn.execute(
                """
                INSERT INTO _schema_registry (dataset, column_json, first_seen, last_seen, primary_key)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (dataset) DO UPDATE SET
                    column_json = excluded.column_json,
                    last_seen = excluded.last_seen,
                    primary_key = excluded.primary_key
                """,
                [dataset, json.dumps(columns), now, now, ",".join(primary_key)],
            )
        if prior is not None and prior != columns:
            return prior
        return None

    def get_schema(self, dataset: Dataset) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT column_json, primary_key FROM _schema_registry WHERE dataset = ?",
                [dataset],
            ).fetchone()
        if not row:
            return None
        return {"columns": json.loads(row[0]), "primary_key": row[1]}

    def ensure_data_table(self, dataset: Dataset, columns: dict[str, str]) -> None:
        """Create the data table if it doesn't exist; widen it if new columns appear.

        Widening (Patch #8): when an incoming file carries columns the existing
        table lacks (e.g. a different feed generation such as the HISTORY
        backfill), ALTER TABLE ADD COLUMN so the values land instead of being
        dropped. Existing rows get NULL for the new columns. This also removes
        the fresh-rebuild race where whichever generation's file happened to
        CREATE the table decided which columns survived.
        """
        if self._read_only:
            raise RuntimeError("read-only warehouse cannot create tables")
        tbl = quote_ident(dataset)
        cols_ddl = ", ".join(f"{quote_ident(name)} {dtype}" for name, dtype in columns.items())
        with self._lock:
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({cols_ddl})")
            existing = {
                r[1]
                for r in self._conn.execute(f"PRAGMA table_info('{dataset}')").fetchall()
            }
            added = []
            for name, dtype in columns.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN {quote_ident(name)} {dtype}"
                    )
                    added.append(name)
            if added:
                logger.info("table_widened", dataset=dataset, added=added)

    # ---------- load ----------

    def upsert_dataframe(
        self,
        dataset: Dataset,
        df: pl.DataFrame,
        *,
        primary_key: tuple[str, ...],
        replace_scope: tuple[str, ...] | None = None,
    ) -> int:
        """Idempotent load: delete-then-insert, wrapped in one transaction.

        Deletion scope (Patch #8): when `replace_scope` is given and its
        columns are present, ALL existing rows whose scope values (e.g. the
        week-end date) appear in the incoming file are deleted — the file is
        treated as the complete extract of its period, so stale rows from a
        different feed generation can't survive alongside it. Otherwise the
        delete is per natural key (`primary_key`).

        Atomicity (Patch #8): DELETE and INSERT run inside a single
        transaction. Without it, an INSERT failure after a committed DELETE
        silently destroys previously-loaded rows — worse, the ledger still
        marks the file that supplied them as 'loaded', so no re-sync ever
        restores them.

        Returns row count. Implemented via DuckDB SQL (no Python iteration).
        """
        if self._read_only:
            raise RuntimeError("read-only warehouse cannot upsert")
        tbl = quote_ident(dataset)
        df_arrow = df.to_arrow()
        with self._lock:
            self._conn.register("incoming_df", df_arrow)
            try:
                # Align columns to existing table; missing ones come in as NULL.
                pragma_rows = self._conn.execute(
                    f"PRAGMA table_info('{dataset}')"
                ).fetchall()
                table_cols = [r[1] for r in pragma_rows]
                # Column type per the TABLE — incoming values are cast to it
                # (Patch #8). Different feed generations type the same logical
                # column differently (e.g. the HISTORY backfill's
                # `fiscal_week_end_date` parses as DATE while the warehouse
                # column is VARCHAR); without the cast, the natural-key DELETE
                # below fails with a binder error on the type mismatch.
                table_types = {r[1]: r[2] for r in pragma_rows}
                if not table_cols:
                    # First load: schema was just created by ensure_data_table.
                    table_cols = df.columns

                def _aligned(col: str) -> str:
                    ident = quote_ident(col)
                    ttype = table_types.get(col)
                    return f"CAST({ident} AS {ttype})" if ttype else ident

                select_exprs = []
                for col in table_cols:
                    if col in df.columns:
                        select_exprs.append(f"{_aligned(col)} AS {quote_ident(col)}")
                    else:
                        select_exprs.append(f"NULL AS {quote_ident(col)}")
                select_sql = ", ".join(select_exprs)

                # Observability (Patch #8): incoming columns absent from the
                # table are dropped by the alignment above. That's the right
                # merge behavior (e.g. HISTORY backfill files carry extra
                # metric columns), but it must never be silent.
                dropped = [c for c in df.columns if c not in table_cols]
                if dropped:
                    logger.warning(
                        "columns_dropped_on_upsert",
                        dataset=dataset,
                        dropped=dropped,
                    )

                # Pick the deletion scope: period-scoped when configured and
                # available, else per natural key. Missing PK columns is a
                # hard error: silently skipping the DELETE would leave INSERT
                # to run unconditionally, which causes silent duplication on
                # every subsequent load (Patch #6.2 — this used to log a
                # warning, which masked the sales_weekly 2.0× bug).
                use_scope = bool(replace_scope) and all(
                    c in df.columns for c in replace_scope
                )
                if replace_scope and not use_scope:
                    logger.warning(
                        "replace_scope_unavailable",
                        dataset=dataset,
                        replace_scope=replace_scope,
                        df_columns=df.columns,
                    )
                if use_scope:
                    # A NULL scope value (e.g. an unparseable week-end date)
                    # can never be matched by a future DELETE — loading it
                    # would duplicate on every re-load. Fail loudly instead.
                    null_pred = " OR ".join(
                        f"{quote_ident(c)} IS NULL" for c in replace_scope
                    )
                    (null_ct,) = self._conn.execute(
                        f"SELECT COUNT(*) FROM incoming_df WHERE {null_pred}"
                    ).fetchone()
                    if null_ct:
                        raise RuntimeError(
                            f"null_replace_scope_values: dataset={dataset!r} "
                            f"replace_scope={replace_scope} null_rows={null_ct} "
                            f"— refusing to load rows whose period columns are "
                            f"NULL; they could never be replaced idempotently."
                        )
                    del_cols_sql = ", ".join(quote_ident(c) for c in replace_scope)
                    del_src_sql = ", ".join(_aligned(c) for c in replace_scope)
                else:
                    missing = [c for c in primary_key if c not in df.columns]
                    if missing:
                        raise RuntimeError(
                            f"primary_key_missing_in_df: dataset={dataset!r} "
                            f"primary_key={primary_key} df_columns={df.columns} "
                            f"missing={missing} — refusing to upsert because the "
                            f"DELETE step cannot run; this would duplicate rows. "
                            f"Fix: add a matching candidate to parsers.PATTERNS "
                            f"primary_key_candidates for this dataset."
                        )
                    del_cols_sql = ", ".join(quote_ident(c) for c in primary_key)
                    # Incoming values are cast to the table's column types so
                    # the tuple comparison binds across feed-generation type
                    # differences (see table_types above).
                    del_src_sql = ", ".join(_aligned(c) for c in primary_key)

                # Atomic delete-then-insert (Patch #8): if the INSERT fails
                # (e.g. an uncastable value in one of the incoming columns),
                # ROLLBACK restores the deleted rows instead of leaving the
                # table silently missing data.
                self._conn.execute("BEGIN TRANSACTION")
                try:
                    self._conn.execute(
                        f"DELETE FROM {tbl} WHERE ({del_cols_sql}) IN "
                        f"(SELECT DISTINCT {del_src_sql} FROM incoming_df)"
                    )
                    self._conn.execute(
                        f"INSERT INTO {tbl} SELECT {select_sql} FROM incoming_df"
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                row_count = self._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            finally:
                self._conn.unregister("incoming_df")
        logger.info("dataframe_loaded", dataset=dataset, rows=df.height, total_rows=row_count)
        return df.height

    # ---------- ledger ----------

    def ledger_seen(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT file_id, file_name, dataset, fingerprint, status, loaded_at "
                "FROM _file_ledger WHERE file_id = ?",
                [file_id],
            ).fetchone()
        if not row:
            return None
        return {
            "file_id": row[0],
            "file_name": row[1],
            "dataset": row[2],
            "fingerprint": row[3],
            "status": row[4],
            "loaded_at": row[5],
        }

    def ledger_upsert(self, row: dict[str, Any]) -> None:
        if self._read_only:
            raise RuntimeError("read-only warehouse cannot write ledger")
        err = row.get("error_message")
        if isinstance(err, str) and len(err) > 2000:
            # Truncate only at 2000 to keep diagnostics readable in the DB.
            err = err[:1997] + "..."
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO _file_ledger
                  (file_id, file_name, folder_id, dataset, file_date, bytes, fingerprint,
                   downloaded_at, loaded_at, row_count, status, error_message, parse_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (file_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    folder_id = excluded.folder_id,
                    dataset = excluded.dataset,
                    file_date = excluded.file_date,
                    bytes = excluded.bytes,
                    fingerprint = excluded.fingerprint,
                    downloaded_at = excluded.downloaded_at,
                    loaded_at = excluded.loaded_at,
                    row_count = excluded.row_count,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    parse_method = excluded.parse_method
                """,
                [
                    row["file_id"],
                    row["file_name"],
                    row.get("folder_id"),
                    row["dataset"],
                    row.get("file_date"),
                    row.get("bytes"),
                    row.get("fingerprint"),
                    row["downloaded_at"],
                    row.get("loaded_at"),
                    row.get("row_count"),
                    row["status"],
                    err,
                    row.get("parse_method"),
                ],
            )

    def log_sync(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        triggered_by: str,
        files_new: int,
        files_loaded: int,
        files_failed: int,
        notes: str = "",
    ) -> None:
        if self._read_only:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO _sync_log (started_at, finished_at, triggered_by, "
                "files_new, files_loaded, files_failed, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    started_at,
                    finished_at,
                    triggered_by,
                    files_new,
                    files_loaded,
                    files_failed,
                    notes,
                ],
            )

    # ---------- views ----------

    def ensure_views(self) -> None:
        if self._read_only:
            return
        # Only create views referencing tables that actually exist.
        existing = {
            r[0]
            for r in self._conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type IN ('BASE TABLE','LOCAL TEMPORARY')"
            ).fetchall()
        }
        with self._lock:
            if "sales_weekly" in existing:
                # Need a `week_end_date` column for this view.
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info('sales_weekly')").fetchall()}
                if "week_end_date" in cols:
                    self._conn.execute(
                        "CREATE OR REPLACE VIEW v_sales_recent_8w AS "
                        "SELECT * FROM sales_weekly "
                        "WHERE week_end_date >= current_date - INTERVAL '8 weeks'"
                    )

    # ---------- describe ----------

    def describe(self) -> dict[str, Any]:
        with self._lock:
            tables = [
                r[0]
                for r in self._conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                ).fetchall()
            ]
            out: dict[str, Any] = {"tables": {}, "views": []}
            for t in tables:
                cols = [
                    {"name": r[1], "type": r[2]}
                    for r in self._conn.execute(f"PRAGMA table_info('{t}')").fetchall()
                ]
                row_count = self._conn.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}").fetchone()[0]
                out["tables"][t] = {"columns": cols, "row_count": row_count}

            views = [
                r[0]
                for r in self._conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'VIEW'"
                ).fetchall()
            ]
            out["views"] = views
        return out

    # ---------- queries ----------

    def execute_sql(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run a single SQL statement. Returns (column_names, rows)."""
        with self._lock:
            cur = self._conn.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cols else []
        return cols, rows

    def detect_date_column(self, table: str) -> str | None:
        """Return the best date column for `table`, or None.

        Priority order (Patch #4, brief Issue 2):
          1. Column with DuckDB type DATE or TIMESTAMP (regardless of name).
          2. Column name ending in `_date` / `_dt` / `_d` (case-insensitive).
          3. Column name containing `date`, `week`, `period`, `as_of`, or `effective`.
          4. Per-dataset registry of canonical names (final fallback).

        Within each priority tier, earlier ordinal_position wins. Target uses the
        `_d` suffix heavily (`fiscal_week_begin_d`, `last_update_d`,
        `processed_ct_d`), so the suffix tier closes the gap that an earlier
        substring-only "date" heuristic missed.
        """
        with self._lock:
            all_cols = self._conn.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
        if not all_cols:
            return None

        # Tier 1: typed DATE/TIMESTAMP.
        for name, dtype in all_cols:
            t = str(dtype).upper()
            if t.startswith("DATE") or t.startswith("TIMESTAMP"):
                return name

        # Tier 2: suffix-style date names.
        for name, _ in all_cols:
            low = name.lower()
            if low.endswith(("_date", "_dt", "_d")):
                return name

        # Tier 3: contains a date-like token.
        DATE_TOKENS = ("date", "week", "period", "as_of", "effective")
        for name, _ in all_cols:
            low = name.lower()
            if any(tok in low for tok in DATE_TOKENS):
                return name

        # Tier 4: per-dataset registry. The COLUMN_ROLES table knows canonical
        # date columns per dataset; consult it last (lowest priority) so the
        # generic heuristic above wins for unknown tables.
        from .column_roles import COLUMN_ROLES

        candidates = COLUMN_ROLES.get(table, {}).get("date", [])
        present = {n.lower() for n, _ in all_cols}
        by_lower = {n.lower(): n for n, _ in all_cols}
        for candidate in candidates:
            if candidate.lower() in present:
                return by_lower[candidate.lower()]
        return None

    def list_datasets(self) -> list[dict[str, Any]]:
        """One row per known dataset table with summary stats."""
        with self._lock:
            tables = {
                r[0]
                for r in self._conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ).fetchall()
            }
            # dict.fromkeys: dedupe while preserving catalog order — multiple
            # patterns may map to one dataset (e.g. the HISTORY backfill).
            datasets = [
                d for d in dict.fromkeys(p.dataset for p in PATTERNS) if d in tables
            ]
            results: list[dict[str, Any]] = []
            for ds in datasets:
                date_col = self.detect_date_column(ds)
                row_count = self._conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(ds)}"
                ).fetchone()[0]
                min_date = max_date = None
                if date_col:
                    md = self._conn.execute(
                        f"SELECT MIN({quote_ident(date_col)}), MAX({quote_ident(date_col)}) "
                        f"FROM {quote_ident(ds)}"
                    ).fetchone()
                    # Coerce ISO-formatted text dates to `date` so callers can
                    # mix-and-match values across datasets that use DATE vs TEXT
                    # columns (Target ships both).
                    min_date = _coerce_date(md[0])
                    max_date = _coerce_date(md[1])
                file_count, last_loaded = self._conn.execute(
                    "SELECT COUNT(*), MAX(loaded_at) FROM _file_ledger "
                    "WHERE dataset = ? AND status = 'loaded'",
                    [ds],
                ).fetchone()
                results.append(
                    {
                        "dataset": ds,
                        "row_count": row_count,
                        "min_date": min_date,
                        "max_date": max_date,
                        "file_count": file_count,
                        "last_loaded_at": last_loaded,
                    }
                )
            return results

    def disk_stats(self) -> dict[str, Any]:
        """Bytes-on-disk + ledger summary for bpd_cache_status."""
        with self._lock:
            n_files, total_bytes = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM _file_ledger"
            ).fetchone()
            last_sync = self._conn.execute(
                "SELECT MAX(finished_at) FROM _sync_log"
            ).fetchone()[0]
        return {
            "ledger_file_count": n_files,
            "ledger_bytes_total": total_bytes,
            "last_sync_finished_at": last_sync,
        }


# --------------------------------------------------------------------------------------
# Read-only view (Patch #3)
# --------------------------------------------------------------------------------------
#
# Earlier patches maintained a `.ro` snapshot file alongside the writable DB so we
# could open one connection RW + one RO. DuckDB 1.5 does not permit mixed RW/RO
# connections to the same file in a single process, so we forked the file. The
# trade-off was schema drift: ALTER TABLE on `bpd.duckdb` didn't propagate to the
# `.ro` copy until a refresh, which surfaced as ghost-old-schema bugs.
#
# Patch #3 eliminates the snapshot. The single writable `Warehouse` is the only
# database file. For engine-level read-only execution, we wrap each query in a
# `BEGIN TRANSACTION READ ONLY` on a fresh cursor, then `ROLLBACK`. DuckDB rejects
# writes inside such a transaction at the engine layer (verified empirically on
# 1.5.2): `TransactionContext Error: Cannot write to database ... — transaction is
# launched in read-only mode`. This:
#
#   * One file, zero divergence between RO and RW views.
#   * Migrations applied via ALTER on the writable conn are visible immediately.
#   * Cursor scoping isolates the RO txn from concurrent writes on other cursors.
#
# The previous `read_only=True` connection mode is still supported by `Warehouse`
# itself, but it's no longer used in production. Tests sometimes open the file
# read-only directly to verify engine-level enforcement on connections that *do*
# refuse writes.


def cleanup_legacy_snapshot(db_path: Path) -> list[Path]:
    """Delete any `.ro` / `.ro.wal` siblings left behind by the patch #1-#2 design.

    Returns the list of paths that were actually removed (for logging by the caller).
    Safe to call when nothing is present.
    """
    removed: list[Path] = []
    for suffix in (db_path.suffix + ".ro", db_path.suffix + ".ro.wal"):
        legacy = db_path.with_suffix(suffix)
        if legacy.exists():
            try:
                legacy.unlink()
                removed.append(legacy)
            except OSError:
                # Best-effort cleanup; if we can't delete, the next startup tries again.
                logger.warning("legacy_snapshot_unlink_failed", path=str(legacy))
    return removed


class ReadOnlyView:
    """Engine-enforced read-only facade over a single writable Warehouse.

    All read queries go through a fresh cursor wrapped in
    `BEGIN TRANSACTION READ ONLY` / `ROLLBACK`. DuckDB rejects writes at the engine
    layer inside such a transaction — this is what makes the facade safe regardless
    of what SQL the caller submits. The `bpd_run_sql` tool layers an application-
    level keyword/AST scan on top as defense-in-depth.

    Methods that read schema/metadata (describe(), detect_date_column()) delegate
    to the underlying Warehouse directly — those are inherently read-only operations
    on information_schema and PRAGMA, so there's no value in wrapping them in a
    transaction.
    """

    def __init__(self, warehouse: Warehouse) -> None:
        if warehouse.read_only:
            # Belt-and-suspenders: the underlying Warehouse should be the writable
            # one. Wrapping a read-only Warehouse would be silly.
            raise RuntimeError(
                "ReadOnlyView expects a writable Warehouse; got read_only=True"
            )
        self._wh = warehouse

    @property
    def read_only(self) -> bool:
        return True

    @property
    def db_path(self) -> Path:
        return self._wh.db_path

    def describe(self) -> dict[str, Any]:
        return self._wh.describe()

    def detect_date_column(self, table: str) -> str | None:
        return self._wh.detect_date_column(table)

    def list_datasets(self) -> list[dict[str, Any]]:
        return self._wh.list_datasets()

    def disk_stats(self) -> dict[str, Any]:
        return self._wh.disk_stats()

    def execute_sql(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run `sql` inside an engine-level read-only transaction.

        Each call gets a fresh cursor + txn so concurrent writes on other cursors
        (e.g. the sync worker) are not blocked. The cursor's RO state is released
        on the ROLLBACK; we never COMMIT (the txn is purely a guard).
        """
        with self._wh._lock:
            cur = self._wh._conn.cursor()
            cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                result = cur.execute(sql)
                cols = [d[0] for d in result.description] if result.description else []
                rows = result.fetchall() if cols else []
            finally:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
            return cols, rows

    def close(self) -> None:
        # The underlying Warehouse owns the connection lifetime; the view is a
        # weakly-owned facade. close() exists for API symmetry with the previous
        # ReadOnlySnapshot but is a no-op.
        pass
