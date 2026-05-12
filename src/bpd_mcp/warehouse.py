"""DuckDB warehouse helpers: bootstrap schema, register schemas, idempotent loads.

DuckDB constraint (same-process): you cannot hold a writable and a read-only connection
to the same file simultaneously. To satisfy §9.3's engine-level read-only requirement
for `bpd_run_sql`, the read-only handle is opened against a *snapshot copy* of the
writable DB (see `ReadOnlySnapshot`). The snapshot refreshes lazily (mtime check) so
queries pick up the latest data after a sync without paying a copy cost on every call.
"""

from __future__ import annotations

import json
import shutil
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

    Pass `read_only=True` to open an engine-level read-only connection. When the
    writable warehouse is already open elsewhere in the process, point this at a
    snapshot file (see `ReadOnlySnapshot`) — DuckDB refuses to mix RW/RO handles
    against the same physical file.
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
        """Create the data table if it doesn't exist, using the discovered schema."""
        if self._read_only:
            raise RuntimeError("read-only warehouse cannot create tables")
        tbl = quote_ident(dataset)
        cols_ddl = ", ".join(f"{quote_ident(name)} {dtype}" for name, dtype in columns.items())
        with self._lock:
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({cols_ddl})")

    # ---------- load ----------

    def upsert_dataframe(
        self,
        dataset: Dataset,
        df: pl.DataFrame,
        *,
        primary_key: tuple[str, ...],
    ) -> int:
        """Idempotent load: delete-then-insert keyed on primary_key.

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
                table_cols = [
                    r[1]
                    for r in self._conn.execute(
                        f"PRAGMA table_info('{dataset}')"
                    ).fetchall()
                ]
                if not table_cols:
                    # First load: schema was just created by ensure_data_table.
                    table_cols = df.columns
                select_exprs = []
                for col in table_cols:
                    if col in df.columns:
                        select_exprs.append(quote_ident(col))
                    else:
                        select_exprs.append(f"NULL AS {quote_ident(col)}")
                select_sql = ", ".join(select_exprs)

                # Delete matching PKs first (idempotent re-load).
                if all(c in df.columns for c in primary_key):
                    pk_cols_sql = ", ".join(quote_ident(c) for c in primary_key)
                    self._conn.execute(
                        f"DELETE FROM {tbl} WHERE ({pk_cols_sql}) IN "
                        f"(SELECT {pk_cols_sql} FROM incoming_df)"
                    )
                else:
                    logger.warning(
                        "primary_key_missing_in_df",
                        dataset=dataset,
                        primary_key=primary_key,
                        df_columns=df.columns,
                    )

                # Insert.
                self._conn.execute(
                    f"INSERT INTO {tbl} SELECT {select_sql} FROM incoming_df"
                )
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

        Type-driven detection: queries information_schema for columns whose data
        type is DATE/TIMESTAMP AND whose name contains 'date'. First match wins.
        Falls back to a per-dataset known-good registry if no type-DATE column
        exists (e.g. Target ships dates as TEXT, which can still be MIN/MAX-ed).
        """
        with self._lock:
            type_match = self._conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                  AND (UPPER(data_type) LIKE 'DATE%' OR UPPER(data_type) LIKE 'TIMESTAMP%')
                  AND LOWER(column_name) LIKE '%date%'
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
            if type_match:
                return type_match[0][0]
            # Type-agnostic name match: any column with 'date' in the name. MIN/MAX
            # still works on TEXT dates if they're ISO-formatted.
            name_match = self._conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                  AND LOWER(column_name) LIKE '%date%'
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
            if name_match:
                return name_match[0][0]

        # Per-dataset fallback registry — used only when introspection finds
        # nothing. These are the canonical primary date columns per dataset.
        fallback = {
            "sales_daily": "sale_date",
            "sales_weekly": "week_end_date",
            "sales_weekly_item": "week_end_date",
            "inventory_daily": "snapshot_date",
            "inventory_weekly": "week_end_date",
            "inventory_weekly_item": "week_end_date",
            "gross_margin": "week_end_date",
            "gross_margin_item": "week_end_date",
            "item_attr": "as_of_date",
            "item_attr_extended": "as_of_date",
            "location_attr": "as_of_date",
            "orders_daily": "order_date",
            "po_plan_daily": "plan_date",
            "po_plan_biweekly": "period_end_date",
            "forecast_weekly": "week_end_date",
        }
        candidate = fallback.get(table)
        if candidate is None:
            return None
        # Only return the fallback if the table actually has that column.
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? AND column_name = ?",
                [table, candidate],
            ).fetchone()
        return candidate if row else None

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
            datasets = [p.dataset for p in PATTERNS if p.dataset in tables]
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
# Read-only snapshot
# --------------------------------------------------------------------------------------


class ReadOnlySnapshot:
    """Manage a snapshot copy of the writable DB for engine-level read-only access.

    Why: DuckDB refuses to open the same file with mixed read/write modes from a
    single process. To get a true `read_only=True` connection for `bpd_run_sql`, we
    copy the writable DB to a snapshot path and open the snapshot read-only.

    The snapshot is refreshed lazily — `Warehouse(read_only=True)` is rebuilt only
    when the source DB's mtime has moved since the last refresh.
    """

    def __init__(self, source_db: Path, snapshot_db: Path | None = None) -> None:
        self._source = source_db
        self._snapshot = snapshot_db or source_db.with_suffix(source_db.suffix + ".ro")
        self._lock = threading.Lock()
        self._wh: Warehouse | None = None
        self._last_source_mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._snapshot

    def _source_mtime(self) -> float | None:
        try:
            return self._source.stat().st_mtime
        except FileNotFoundError:
            return None

    def get(self) -> Warehouse:
        """Return a read-only Warehouse on a fresh-enough snapshot."""
        with self._lock:
            src_mtime = self._source_mtime()
            stale = (
                self._wh is None
                or self._last_source_mtime is None
                or (src_mtime is not None and src_mtime != self._last_source_mtime)
            )
            if stale:
                if self._wh is not None:
                    self._wh.close()
                    self._wh = None
                if src_mtime is None:
                    # No source DB yet. Make an empty snapshot so reads still work.
                    self._snapshot.parent.mkdir(parents=True, exist_ok=True)
                    empty = duckdb.connect(str(self._snapshot))
                    empty.execute(METADATA_DDL)
                    empty.close()
                else:
                    # Copy source -> snapshot. Use shutil (atomic on POSIX via rename).
                    self._snapshot.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self._snapshot.with_suffix(self._snapshot.suffix + ".tmp")
                    shutil.copy2(self._source, tmp)
                    tmp.replace(self._snapshot)
                self._wh = Warehouse(self._snapshot, read_only=True)
                self._last_source_mtime = src_mtime
                logger.info(
                    "ro_snapshot_refreshed",
                    source=str(self._source),
                    snapshot=str(self._snapshot),
                    mtime=src_mtime,
                )
            assert self._wh is not None
            return self._wh

    def close(self) -> None:
        with self._lock:
            if self._wh is not None:
                self._wh.close()
                self._wh = None
