"""Engine-level read-only enforcement tests for ReadOnlyView (Patch #3).

The previous design used a `.duckdb.ro` snapshot which drifted on migrations. The
new design wraps each query in `BEGIN TRANSACTION READ ONLY` on a cursor from the
single writable connection. DuckDB itself rejects writes — verified here for every
DDL/DML keyword.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bpd_mcp.warehouse import ReadOnlyView, Warehouse, cleanup_legacy_snapshot


def test_read_only_view_rejects_writes_at_engine_layer(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT); INSERT INTO t VALUES (1), (2)")
    ro = ReadOnlyView(wh)
    try:
        # Read works.
        _, rows = ro.execute_sql("SELECT COUNT(*) FROM t")
        assert rows[0][0] == 2
        # Each DDL/DML attempt is rejected by DuckDB at the engine layer.
        for write_sql in (
            "INSERT INTO t VALUES (3)",
            "UPDATE t SET x = 99",
            "DELETE FROM t",
            "CREATE TABLE u (a INT)",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN y INT",
        ):
            with pytest.raises(duckdb.Error):
                ro.execute_sql(write_sql)
        # Confirm no side effects leaked.
        _, after = ro.execute_sql("SELECT COUNT(*) FROM t")
        assert after[0][0] == 2
    finally:
        wh.close()


def test_read_only_view_refuses_to_wrap_read_only_warehouse(tmp_path: Path) -> None:
    """ReadOnlyView only makes sense over a writable Warehouse."""
    rw = Warehouse(tmp_path / "bpd.duckdb")
    rw.execute_sql("CREATE TABLE t(x INT)")
    rw.close()
    ro_wh = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        with pytest.raises(RuntimeError):
            ReadOnlyView(ro_wh)
    finally:
        ro_wh.close()


def test_read_only_view_delegates_describe(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE sales_weekly (tcin BIGINT, week_end_date DATE)")
    ro = ReadOnlyView(wh)
    try:
        info = ro.describe()
        assert "sales_weekly" in info["tables"]
    finally:
        wh.close()


def test_read_only_view_concurrent_with_writable_cursor(tmp_path: Path) -> None:
    """RO transaction on one cursor must not block writes on the warehouse's cursor."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t(x INT); INSERT INTO t VALUES (1)")
    ro = ReadOnlyView(wh)
    try:
        _, rows = ro.execute_sql("SELECT COUNT(*) FROM t")
        assert rows[0][0] == 1
        # Writable side can still insert; the RO txn was scoped to the RO cursor and
        # was ROLLBACK'd before this point.
        wh.execute_sql("INSERT INTO t VALUES (2), (3)")
        _, rows = ro.execute_sql("SELECT COUNT(*) FROM t")
        assert rows[0][0] == 3
    finally:
        wh.close()


def test_no_ro_path_referenced_in_active_codepath(tmp_path: Path) -> None:
    """Verify the warehouse module no longer creates a .ro file at runtime.

    We build a fresh warehouse, run a migration, then assert no .ro sibling exists.
    The .ro snapshot design from patches #1-#2 has been retired.
    """
    db = tmp_path / "bpd.duckdb"
    wh = Warehouse(db)
    try:
        wh.execute_sql("CREATE TABLE probe (x INT)")
    finally:
        wh.close()
    assert not (tmp_path / "bpd.duckdb.ro").exists()
    assert not (tmp_path / "bpd.duckdb.ro.wal").exists()


def test_cleanup_legacy_snapshot_removes_stale_files(tmp_path: Path) -> None:
    db = tmp_path / "bpd.duckdb"
    db.write_bytes(b"real db (placeholder)")
    legacy = tmp_path / "bpd.duckdb.ro"
    legacy_wal = tmp_path / "bpd.duckdb.ro.wal"
    legacy.write_bytes(b"stale ro")
    legacy_wal.write_bytes(b"stale wal")

    removed = cleanup_legacy_snapshot(db)
    assert sorted(p.name for p in removed) == ["bpd.duckdb.ro", "bpd.duckdb.ro.wal"]
    assert not legacy.exists()
    assert not legacy_wal.exists()
    # Main DB untouched.
    assert db.exists()


def test_cleanup_legacy_snapshot_idempotent_when_nothing_present(tmp_path: Path) -> None:
    db = tmp_path / "bpd.duckdb"
    removed = cleanup_legacy_snapshot(db)
    assert removed == []


async def test_run_sql_validator_blocks_attach_even_when_engine_wouldnt(
    tmp_path: Path,
) -> None:
    """ATTACH bypasses DuckDB's BEGIN TRANSACTION READ ONLY at the engine layer.

    Our SQL safety validator catches it at the token-scan layer instead. This
    test makes that two-layer story explicit: ReadOnlyView would accept ATTACH,
    but `bpd_run_sql` rejects it before the engine ever sees it.
    """
    from bpd_mcp.schemas import RunSqlInput
    from bpd_mcp.tools.query import run_sql

    wh = Warehouse(tmp_path / "bpd.duckdb")
    ro = ReadOnlyView(wh)
    try:
        # Validator must reject ATTACH and COPY at the input layer.
        for blocked in (
            f"ATTACH '{tmp_path / 'side.duckdb'}' AS side",
            f"COPY (SELECT 1) TO '{tmp_path / 'leak.csv'}'",
            "INSTALL httpfs",
            "LOAD httpfs",
            "EXPORT DATABASE '/tmp/x'",
        ):
            resp = await run_sql(ro, RunSqlInput(sql=blocked))
            assert resp.ok is False, f"validator failed to block: {blocked!r}"
            assert resp.error.code == "SQL_BLOCKED", (
                f"expected SQL_BLOCKED for {blocked!r}, got {resp.error.code}"
            )
    finally:
        wh.close()


def test_read_only_view_sees_migrations_immediately(tmp_path: Path) -> None:
    """Patch #3 invariant: migrations on rw are visible to ro on the next query.

    Reproduces the bug that motivated this patch — a column added via ALTER on the
    writable connection was not visible to bpd_run_sql because the .ro snapshot was
    stale. With ReadOnlyView there's no snapshot, so the read sees the new column.
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    ro = ReadOnlyView(wh)
    try:
        wh.execute_sql("ALTER TABLE _file_ledger ADD COLUMN IF NOT EXISTS extra_col TEXT")
        _, rows = ro.execute_sql("PRAGMA table_info('_file_ledger')")
        cols = {r[1] for r in rows}
        assert "extra_col" in cols
    finally:
        wh.close()
