"""SQL-safety tests — both the keyword/AST layer AND the engine layer.

§13 mandates that we verify:
  * Every form of write attempt is rejected by the validator.
  * The actual connection used by run_sql is opened read_only=True (so even if
    something slipped past validation, the engine refuses).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bpd_mcp.schemas import RunSqlInput
from bpd_mcp.sql_safety import SqlBlocked, validate, wrap_with_limit
from bpd_mcp.tools.query import run_sql
from bpd_mcp.warehouse import Warehouse

# ---------- validator (layer 1-3) ----------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales_daily VALUES (1)",
        "UPDATE sales_daily SET units_sold = 0",
        "DELETE FROM sales_daily",
        "CREATE TABLE x AS SELECT 1",
        "DROP TABLE sales_daily",
        "ALTER TABLE sales_daily ADD COLUMN x INT",
        "TRUNCATE TABLE sales_daily",
        "ATTACH 'evil.db' AS evil",
        "COPY sales_daily TO '/tmp/leak.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CALL pragma_database_list()",
        "VACUUM",
        "SELECT 1; DELETE FROM sales_daily",
        "/* sneaky */ DROP TABLE sales_daily",
        "/* multi\nline */ INSERT INTO _file_ledger VALUES (1)",
        "-- comment\nDROP TABLE sales_daily",
        "SELECT 1 -- ; DROP TABLE x\n; DELETE FROM sales_daily",
        "BEGIN; SELECT 1; COMMIT",
        "PRAGMA enable_external_access = 1",
    ],
)
def test_validator_rejects_writes(sql: str) -> None:
    with pytest.raises(SqlBlocked):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM sales_daily WHERE tcin = 1",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "EXPLAIN SELECT 1",
        "PRAGMA table_info('sales_daily')",
        "SHOW TABLES",
        "SELECT '/* not a comment */' AS s",
        "SELECT 'has ; in string' AS s",
    ],
)
def test_validator_accepts_reads(sql: str) -> None:
    assert validate(sql)


def test_wrap_with_limit_uses_subquery() -> None:
    out = wrap_with_limit("SELECT * FROM sales_daily LIMIT 5", 100)
    assert "LIMIT 100" in out
    # Original LIMIT survives inside the subquery.
    assert "LIMIT 5" in out


# ---------- engine-level enforcement (layer 4) ----------


def test_run_sql_connection_is_read_only(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE sales_daily (tcin BIGINT)")
    wh.execute_sql("INSERT INTO sales_daily VALUES (1)")
    wh.close()
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        assert ro.read_only is True
        # Reads work.
        _, rows = ro.execute_sql("SELECT COUNT(*) FROM sales_daily")
        assert rows[0][0] == 1
        # Writes must be rejected by the engine, not us.
        with pytest.raises(duckdb.Error):
            ro.execute_sql("INSERT INTO sales_daily VALUES (2)")
    finally:
        ro.close()


async def test_run_sql_tool_rejects_writes(tmp_path: Path) -> None:
    """End-to-end: the run_sql tool returns SQL_BLOCKED for write attempts."""
    rw = Warehouse(tmp_path / "bpd.duckdb")
    rw.execute_sql("CREATE TABLE sales_daily (tcin BIGINT)")
    rw.execute_sql("INSERT INTO sales_daily VALUES (1)")
    rw.close()
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        for bad_sql in [
            "DROP TABLE sales_daily",
            "INSERT INTO sales_daily VALUES (99)",
            "SELECT 1; DELETE FROM sales_daily",
            "/*x*/ DROP TABLE sales_daily",
        ]:
            resp = await run_sql(ro, RunSqlInput(sql=bad_sql))
            assert resp.ok is False
            assert resp.error is not None
            assert resp.error.code == "SQL_BLOCKED", f"failed to block: {bad_sql!r}"
    finally:
        ro.close()


async def test_run_sql_tool_executes_select(tmp_path: Path) -> None:
    rw = Warehouse(tmp_path / "bpd.duckdb")
    rw.execute_sql("CREATE TABLE sales_daily (tcin BIGINT, units BIGINT)")
    rw.execute_sql("INSERT INTO sales_daily VALUES (1, 10), (2, 20), (3, 30)")
    rw.close()
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        resp = await run_sql(ro, RunSqlInput(sql="SELECT SUM(units) AS total FROM sales_daily"))
        assert resp.ok is True
        # rows list from the wrapped LIMIT'd subquery.
        rows = resp.data["rows"]
        assert rows[0]["total"] == 60
    finally:
        ro.close()


async def test_run_sql_tool_refuses_writable_connection(tmp_path: Path) -> None:
    """Belt-and-suspenders: passing a writable Warehouse to run_sql is rejected outright."""
    rw = Warehouse(tmp_path / "bpd.duckdb")
    try:
        resp = await run_sql(rw, RunSqlInput(sql="SELECT 1"))
        assert resp.ok is False
        assert resp.error.code == "SQL_BLOCKED"
    finally:
        rw.close()
