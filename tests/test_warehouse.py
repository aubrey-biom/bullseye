"""Warehouse tests: idempotent loads, schema drift detection, view creation."""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from bpd_mcp.parsers import derive_duckdb_schema
from bpd_mcp.warehouse import Warehouse


def _zip(path: Path, body: str, inner: str = "data.txt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, body)
    return path


def test_metadata_tables_created(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        _, rows = wh.execute_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        )
        names = {r[0] for r in rows}
        assert "_file_ledger" in names
        assert "_sync_log" in names
        assert "_schema_registry" in names
    finally:
        wh.close()


def test_idempotent_load(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    df = pl.DataFrame(
        {
            "tcin": [1, 1, 2],
            "location_id": [100, 200, 100],
            "sale_date": [date(2026, 4, 21), date(2026, 4, 21), date(2026, 4, 21)],
            "units_sold": [10, 20, 5],
        }
    )
    cols = derive_duckdb_schema(df)
    try:
        wh.register_schema("sales_daily", cols, ("tcin", "location_id", "sale_date"))
        wh.ensure_data_table("sales_daily", cols)
        wh.upsert_dataframe("sales_daily", df, primary_key=("tcin", "location_id", "sale_date"))
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_daily")
        assert rows[0][0] == 3

        # Re-load the same df — count must not change.
        wh.upsert_dataframe("sales_daily", df, primary_key=("tcin", "location_id", "sale_date"))
        _, rows2 = wh.execute_sql("SELECT COUNT(*) FROM sales_daily")
        assert rows2[0][0] == 3

        # Update one row's metric — total still 3 rows.
        df2 = df.with_columns(pl.when(pl.col("tcin") == 1).then(99).otherwise(pl.col("units_sold")).alias("units_sold"))
        wh.upsert_dataframe("sales_daily", df2, primary_key=("tcin", "location_id", "sale_date"))
        _, rows3 = wh.execute_sql("SELECT COUNT(*) FROM sales_daily")
        assert rows3[0][0] == 3
        _, total_units = wh.execute_sql("SELECT SUM(units_sold) FROM sales_daily")
        # tcin=1 had 10+20, now 99+99; tcin=2 still 5. Total = 99+99+5 = 203.
        assert total_units[0][0] == 203
    finally:
        wh.close()


def test_schema_drift_detected(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        cols_v1 = {"tcin": "BIGINT", "sale_date": "DATE", "units_sold": "BIGINT"}
        prior = wh.register_schema("sales_daily", cols_v1, ("tcin", "sale_date"))
        assert prior is None  # first time
        cols_v2 = {"tcin": "BIGINT", "sale_date": "DATE", "units_sold": "BIGINT", "promo_flag": "BIGINT"}
        prior = wh.register_schema("sales_daily", cols_v2, ("tcin", "sale_date"))
        assert prior == cols_v1
    finally:
        wh.close()


def test_view_creation_only_when_columns_present(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # Without sales_weekly table, ensure_views is a no-op (no crash).
        wh.ensure_views()
        # Create sales_weekly with week_end_date and verify view appears.
        cols = {
            "tcin": "BIGINT",
            "location_id": "BIGINT",
            "week_end_date": "DATE",
            "units_sold": "BIGINT",
        }
        wh.register_schema("sales_weekly", cols, ("tcin", "location_id", "week_end_date"))
        wh.ensure_data_table("sales_weekly", cols)
        wh.ensure_views()
        _, rows = wh.execute_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
        )
        assert any(r[0] == "v_sales_recent_8w" for r in rows)
    finally:
        wh.close()


def test_ledger_upsert_and_seen(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        assert wh.ledger_seen("fid-1") is None
        wh.ledger_upsert(
            {
                "file_id": "fid-1",
                "file_name": "test.zip",
                "folder_id": "fold-1",
                "dataset": "sales_daily",
                "file_date": date(2026, 4, 21),
                "bytes": 1024,
                "fingerprint": "abc",
                "downloaded_at": datetime(2026, 4, 22, tzinfo=UTC),
                "loaded_at": datetime(2026, 4, 22, tzinfo=UTC),
                "row_count": 100,
                "status": "loaded",
            }
        )
        row = wh.ledger_seen("fid-1")
        assert row is not None
        assert row["status"] == "loaded"
        assert row["dataset"] == "sales_daily"
    finally:
        wh.close()


def test_describe_lists_tables_and_columns(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        cols = {"tcin": "BIGINT", "sale_date": "DATE", "units_sold": "BIGINT"}
        wh.register_schema("sales_daily", cols, ("tcin", "sale_date"))
        wh.ensure_data_table("sales_daily", cols)
        info = wh.describe()
        assert "sales_daily" in info["tables"]
        assert {c["name"] for c in info["tables"]["sales_daily"]["columns"]} == set(cols)
    finally:
        wh.close()
