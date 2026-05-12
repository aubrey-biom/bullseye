"""Tests for bpd_export_query_to_csv (Patch #4, Issue 5)."""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from pydantic import SecretStr

from bpd_mcp.config import Settings
from bpd_mcp.schemas import ExportQueryToCsvInput
from bpd_mcp.tools.query import export_query_to_csv
from bpd_mcp.warehouse import ReadOnlyView, Warehouse


def _settings(tmp_path: Path) -> Settings:
    s = Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("p"),
        bpd_data_dir=str(tmp_path),
    )
    s.ensure_dirs()
    return s


async def test_export_query_to_csv_happy_path(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE sales_weekly (tcin BIGINT, sale_quantity BIGINT)")
    wh.execute_sql("INSERT INTO sales_weekly VALUES (100, 50), (200, 30), (300, 10)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        resp = await export_query_to_csv(
            ro,
            s,
            ExportQueryToCsvInput(
                sql="SELECT tcin, sale_quantity FROM sales_weekly ORDER BY tcin",
                filename="top_skus.csv",
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error

    p = Path(resp.data["path"])
    assert p.exists()
    assert p.name == "top_skus.csv"
    assert p.parent == s.data_dir / "exports"
    assert resp.data["rows_written"] == 3
    assert resp.data["columns"] == ["tcin", "sale_quantity"]
    assert resp.data["bytes_written"] > 0

    # Re-read the CSV and confirm contents.
    with p.open() as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["tcin", "sale_quantity"]
    # Numbers come back as strings via csv.reader, no big deal.
    assert rows[1] == ["100", "50"]
    assert rows[2] == ["200", "30"]
    assert rows[3] == ["300", "10"]


async def test_export_query_to_csv_no_header(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT); INSERT INTO t VALUES (1), (2)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        resp = await export_query_to_csv(
            ro,
            s,
            ExportQueryToCsvInput(
                sql="SELECT * FROM t",
                filename="bare.csv",
                include_header=False,
                response_format="json",
            ),
        )
    finally:
        wh.close()
    p = Path(resp.data["path"])
    with p.open() as f:
        rows = list(csv.reader(f))
    assert rows == [["1"], ["2"]]


async def test_export_query_to_csv_rejects_write_sql(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        for blocked in (
            "DROP TABLE t",
            "INSERT INTO t VALUES (99)",
            "DELETE FROM t",
            "CREATE TABLE evil AS SELECT 1",
            "ATTACH '/tmp/other.duckdb' AS o",
            "COPY t TO '/tmp/leak.csv'",
            "SELECT 1; DROP TABLE t",
        ):
            resp = await export_query_to_csv(
                ro,
                s,
                ExportQueryToCsvInput(
                    sql=blocked, filename="x.csv", response_format="json"
                ),
            )
            assert resp.ok is False, f"failed to block: {blocked!r}"
            assert resp.error.code == "SQL_BLOCKED"
    finally:
        wh.close()


async def test_export_query_to_csv_rejects_path_in_filename(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT); INSERT INTO t VALUES (1)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        for bad in ("../escape.csv", "subdir/file.csv", "/tmp/abs.csv"):
            resp = await export_query_to_csv(
                ro,
                s,
                ExportQueryToCsvInput(
                    sql="SELECT * FROM t",
                    filename=bad,
                    response_format="json",
                ),
            )
            assert resp.ok is False, f"failed to reject: {bad!r}"
            assert resp.error.code == "INVALID_FILENAME"
    finally:
        wh.close()


async def test_export_query_to_csv_rejects_non_csv_extension(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT); INSERT INTO t VALUES (1)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        for bad in ("results.txt", "data.json", "results", ".csv"):
            resp = await export_query_to_csv(
                ro,
                s,
                ExportQueryToCsvInput(
                    sql="SELECT * FROM t",
                    filename=bad,
                    response_format="json",
                ),
            )
            assert resp.ok is False, f"failed to reject: {bad!r}"
            assert resp.error.code == "INVALID_FILENAME"
    finally:
        wh.close()


async def test_export_query_to_csv_file_mode_is_0644(tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        return  # POSIX-only
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE t (x INT); INSERT INTO t VALUES (1)")
    ro = ReadOnlyView(wh)
    s = _settings(tmp_path)
    try:
        resp = await export_query_to_csv(
            ro,
            s,
            ExportQueryToCsvInput(
                sql="SELECT * FROM t", filename="perm.csv", response_format="json"
            ),
        )
    finally:
        wh.close()
    p = Path(resp.data["path"])
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o644
