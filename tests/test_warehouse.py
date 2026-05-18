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


def test_sales_weekly_idempotent_with_multi_channel_rows_per_pk(tmp_path: Path) -> None:
    """sales_weekly carries multiple rows per (tcin, location_id, week_end_date)
    split by channel/fulfillment. The upsert's primary_key is just (tcin,
    location_id, week_end_date), so the DELETE removes the full set of rows for
    that key and the INSERT re-adds whichever rows are in the new df. Re-loading
    the SAME df twice must leave the row count unchanged regardless of how
    many channel splits exist per natural key — and the full row content must
    be preserved verbatim (Patch #6.1 regression guard for the 2.0× duplication
    reported during sales_weekly validation).
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "week_end_date")
    # Two channel splits per (tcin, location_id, week_end_date): in-store and online.
    df = pl.DataFrame(
        {
            "tcin": [100, 100, 200, 200],
            "location_id": [1234, 1234, 1234, 1234],
            "week_end_date": [
                date(2026, 5, 9),
                date(2026, 5, 9),
                date(2026, 5, 9),
                date(2026, 5, 9),
            ],
            "reporting_channel": ["store", "online", "store", "online"],
            "units_sold": [10, 3, 5, 2],
            "sales_dollars": [100.0, 30.0, 50.0, 20.0],
        }
    )
    cols = derive_duckdb_schema(df)
    try:
        wh.register_schema("sales_weekly", cols, pk)
        wh.ensure_data_table("sales_weekly", cols)
        wh.upsert_dataframe("sales_weekly", df, primary_key=pk)
        _, after_first = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert after_first[0][0] == 4

        # Re-load the SAME df. Row count must NOT double; channel splits intact.
        wh.upsert_dataframe("sales_weekly", df, primary_key=pk)
        _, after_second = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert after_second[0][0] == 4, "re-loading the same df must not duplicate"
        _, dollars = wh.execute_sql("SELECT SUM(sales_dollars) FROM sales_weekly")
        assert dollars[0][0] == 200.0  # 100 + 30 + 50 + 20

        # Sanity: no literal-row duplicates exist after re-load.
        _, dup_check = wh.execute_sql(
            "SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * FROM sales_weekly)) "
            "FROM sales_weekly"
        )
        assert dup_check[0][0] == 0
    finally:
        wh.close()


def test_sales_weekly_two_different_files_with_overlapping_rows_dedupe(
    tmp_path: Path,
) -> None:
    """Patch #6.1 forensic guard. Address the post-merge audit question: if two
    DIFFERENT files (different file_ids, potentially different metric values)
    cover the same (tcin, location_id, week_end_date) tuples, the second load
    must REPLACE the first's rows — not append. This is the scenario that a
    file_id-keyed upsert would silently fail (each file gets its own row set,
    no deletion across files); a natural-key-keyed upsert handles it correctly.

    `test_sales_weekly_idempotent_with_multi_channel_rows_per_pk` only proves
    same-df-twice. This proves two-different-dfs-with-overlap.
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "week_end_date")
    df_a = pl.DataFrame(
        {
            "tcin": [100, 100, 200],
            "location_id": [1234, 1234, 1234],
            "week_end_date": [date(2026, 5, 9), date(2026, 5, 9), date(2026, 5, 9)],
            "reporting_channel": ["store", "online", "store"],
            "units_sold": [10, 3, 5],
            "sales_dollars": [100.0, 30.0, 50.0],
        }
    )
    # df_b: SAME (tcin, location_id, week_end_date) tuples but updated metric values
    # — exactly what a Kiteworks-repackaged file looks like.
    df_b = pl.DataFrame(
        {
            "tcin": [100, 100, 200],
            "location_id": [1234, 1234, 1234],
            "week_end_date": [date(2026, 5, 9), date(2026, 5, 9), date(2026, 5, 9)],
            "reporting_channel": ["store", "online", "store"],
            "units_sold": [11, 4, 6],  # updated values
            "sales_dollars": [110.0, 40.0, 60.0],
        }
    )
    cols = derive_duckdb_schema(df_a)
    try:
        wh.register_schema("sales_weekly", cols, pk)
        wh.ensure_data_table("sales_weekly", cols)
        wh.upsert_dataframe("sales_weekly", df_a, primary_key=pk)
        wh.upsert_dataframe("sales_weekly", df_b, primary_key=pk)

        # Three rows total (df_a's are deleted before df_b's INSERT) — not six.
        _, total = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert total[0][0] == 3, (
            "natural-key upsert must replace overlapping rows across files; "
            "got duplication implying file_id-keyed semantics"
        )
        # Verify df_b's updated values won — not df_a's.
        _, dollars = wh.execute_sql("SELECT SUM(sales_dollars) FROM sales_weekly")
        assert dollars[0][0] == 210.0  # 110 + 40 + 60, NOT 180 (df_a)

        # And no literal-row dups.
        _, dup_check = wh.execute_sql(
            "SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * FROM sales_weekly)) "
            "FROM sales_weekly"
        )
        assert dup_check[0][0] == 0
    finally:
        wh.close()


def test_upsert_into_existing_bool_column_with_nulls(tmp_path: Path) -> None:
    """Patch #6 integration regression. Once parsers map `""` to NULL, the df
    arrives at the warehouse as Boolean with None for missing rows. DuckDB must
    accept that mix into an existing BOOLEAN column without ConversionException.
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        cols = {"tcin": "BIGINT", "purchase_order_active_f": "BOOLEAN"}
        wh.ensure_data_table("orders_daily", cols)
        df = pl.DataFrame(
            {
                "tcin": [100, 200, 300],
                "purchase_order_active_f": [True, None, False],
            }
        )
        rows = wh.upsert_dataframe("orders_daily", df, primary_key=("tcin",))
        assert rows == 3
        _, fetched = wh.execute_sql(
            "SELECT tcin, purchase_order_active_f FROM orders_daily ORDER BY tcin"
        )
        assert fetched == [(100, True), (200, None), (300, False)]
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


# ---------- Patch #2: error_message / parse_method columns + migration safety ----------


def test_ledger_has_error_message_and_parse_method_columns(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        _, rows = wh.execute_sql("PRAGMA table_info('_file_ledger')")
        cols = {r[1] for r in rows}
        assert "error_message" in cols
        assert "parse_method" in cols
    finally:
        wh.close()


def test_migration_idempotent_on_existing_warehouse(tmp_path: Path) -> None:
    """Opening a warehouse twice should not crash on the ALTER ADD COLUMN IF NOT EXISTS."""
    db_path = tmp_path / "bpd.duckdb"
    wh1 = Warehouse(db_path)
    wh1.close()
    # Second open re-runs DDL + migrations. Must not throw.
    wh2 = Warehouse(db_path)
    try:
        _, rows = wh2.execute_sql("PRAGMA table_info('_file_ledger')")
        cols = {r[1] for r in rows}
        assert "error_message" in cols
        assert "parse_method" in cols
    finally:
        wh2.close()


def test_ledger_persists_error_message_and_parse_method(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        from datetime import UTC, datetime

        wh.ledger_upsert(
            {
                "file_id": "fid-fail",
                "file_name": "broken.zip",
                "folder_id": "fold-1",
                "dataset": "sales_daily",
                "file_date": None,
                "bytes": 100,
                "fingerprint": "xx",
                "downloaded_at": datetime.now(UTC),
                "loaded_at": None,
                "row_count": None,
                "status": "failed",
                "error_message": "ParseError: bogus data on line 5",
                "parse_method": "failed",
            }
        )
        _, rows = wh.execute_sql(
            "SELECT error_message, parse_method, status FROM _file_ledger WHERE file_id = 'fid-fail'"
        )
        assert rows[0][0] == "ParseError: bogus data on line 5"
        assert rows[0][1] == "failed"
        assert rows[0][2] == "failed"
    finally:
        wh.close()


def test_ledger_error_message_truncates_at_2000(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        from datetime import UTC, datetime

        huge = "x" * 5000
        wh.ledger_upsert(
            {
                "file_id": "fid-huge",
                "file_name": "huge.zip",
                "folder_id": "fold-1",
                "dataset": "sales_daily",
                "file_date": None,
                "bytes": 100,
                "fingerprint": "xx",
                "downloaded_at": datetime.now(UTC),
                "loaded_at": None,
                "row_count": None,
                "status": "failed",
                "error_message": huge,
                "parse_method": "failed",
            }
        )
        _, rows = wh.execute_sql(
            "SELECT LENGTH(error_message), error_message FROM _file_ledger WHERE file_id = 'fid-huge'"
        )
        assert rows[0][0] == 2000  # truncated
        assert rows[0][1].endswith("...")
    finally:
        wh.close()


def test_detect_date_column_prefers_typed_date_over_text(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # Create a table with both a typed DATE column and a text-typed *_date column.
        wh.execute_sql(
            "CREATE TABLE sales_daily (tcin BIGINT, sale_date DATE, processed_date TEXT)"
        )
        # The DATE-typed sale_date should win over the TEXT processed_date.
        assert wh.detect_date_column("sales_daily") == "sale_date"
    finally:
        wh.close()


def test_detect_date_column_falls_back_to_text_name_match(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # Only a text-typed *_date column. detect_date_column should still find it.
        wh.execute_sql("CREATE TABLE orders_daily (tcin BIGINT, order_date TEXT)")
        assert wh.detect_date_column("orders_daily") == "order_date"
    finally:
        wh.close()


def test_detect_date_column_returns_none_when_absent(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.execute_sql("CREATE TABLE foo (a BIGINT, b TEXT)")
        assert wh.detect_date_column("foo") is None
    finally:
        wh.close()
