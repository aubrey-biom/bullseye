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


def test_upsert_raises_on_missing_primary_key_columns(tmp_path: Path) -> None:
    """Patch #6.2 hard-fail contract. If any PK column is missing from the df,
    upsert MUST raise instead of silently skipping DELETE and running INSERT
    unconditionally. The old warn-and-skip behavior masked the sales_weekly
    2.0× duplication bug.
    """
    import pytest

    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        cols = {"tcin": "BIGINT", "location_id": "BIGINT", "units_sold": "BIGINT"}
        wh.ensure_data_table("sales_daily", cols)
        df = pl.DataFrame({"tcin": [1, 2], "units_sold": [10, 20]})  # NO location_id
        with pytest.raises(RuntimeError, match="primary_key_missing_in_df"):
            wh.upsert_dataframe(
                "sales_daily",
                df,
                primary_key=("tcin", "location_id", "sales_date"),
            )
        # Table is untouched.
        _, total = wh.execute_sql("SELECT COUNT(*) FROM sales_daily")
        assert total[0][0] == 0
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


# ---------- Patch #7: natural-key idempotency for po_plan_daily / gross_margin / gross_margin_item ----------


def test_po_plan_daily_natural_key_idempotency(tmp_path: Path) -> None:
    """Patch #7. Natural PK is (tcin, business_d, order_d, receiving_location_id).
    Verified empirically against the live warehouse: COUNT(*) == COUNT(DISTINCT NK)
    == 869,580. This test locks in the contract with a 4-row fixture covering
    multiple business_d × order_d × receiving_location_id combinations for the
    same tcin, plus a re-load + cross-file overlap check.
    """
    from datetime import date as _date

    pk = ("tcin", "business_d", "order_d", "receiving_location_id")
    df_a = pl.DataFrame(
        {
            "tcin": [100, 100, 100, 200],
            "business_d": [
                _date(2026, 5, 19), _date(2026, 5, 19),
                _date(2026, 5, 20), _date(2026, 5, 19),
            ],
            "order_d": [
                _date(2026, 5, 25), _date(2026, 5, 25),
                _date(2026, 5, 25), _date(2026, 5, 26),
            ],
            # Same tcin/business_d/order_d but different receiving locations
            # must NOT collapse (key includes location).
            "receiving_location_id": [1234, 5678, 1234, 1234],
            "planned_units": [50, 30, 40, 20],
        }
    )
    cols = derive_duckdb_schema(df_a)
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.ensure_data_table("po_plan_daily", cols)
        wh.upsert_dataframe("po_plan_daily", df_a, primary_key=pk)
        # COUNT(*) == COUNT(DISTINCT NK) — the contract.
        _, ck = wh.execute_sql(
            "SELECT COUNT(*), COUNT(DISTINCT (tcin, business_d, order_d, "
            "receiving_location_id)) FROM po_plan_daily"
        )
        assert ck[0][0] == ck[0][1] == 4

        # Re-load with updated metric values for two of the NKs — count stays at 4.
        df_b = df_a.with_columns(
            (pl.col("planned_units") + 5).alias("planned_units")
        )
        wh.upsert_dataframe("po_plan_daily", df_b, primary_key=pk)
        _, ck2 = wh.execute_sql(
            "SELECT COUNT(*), SUM(planned_units) FROM po_plan_daily"
        )
        assert ck2[0][0] == 4
        # df_b's updated values won: (50+5)+(30+5)+(40+5)+(20+5) = 160.
        assert ck2[0][1] == 160
    finally:
        wh.close()


def test_gross_margin_natural_key_idempotency(tmp_path: Path) -> None:
    """Patch #7. Natural PK is 8 cols:
    (tcin, location_id, location_id_originated, fiscal_week_end_d,
     channel_originated, channel_fulfilled, fulfillment_type, fulfillment_subtype).
    Verified empirically: 197,013 = 197,013. Critically, dropping
    `location_id_originated` (the 7-col version) would silently lose 3,831 rows.
    """
    from datetime import date as _date

    pk = (
        "tcin", "location_id", "location_id_originated", "fiscal_week_end_d",
        "channel_originated", "channel_fulfilled",
        "fulfillment_type", "fulfillment_subtype",
    )
    # Same (tcin, location_id, fiscal_week_end_d) tuple split across
    # channel/fulfillment combos AND across two origination locations —
    # the row that distinguishes location_id != location_id_originated is the
    # regression case for the 3,831-row data-loss scenario the user flagged.
    df = pl.DataFrame(
        {
            "tcin": [100, 100, 100, 100, 100],
            "location_id": [1234, 1234, 1234, 1234, 1234],
            "location_id_originated": [1234, 1234, 1234, 1234, 5678],
            "fiscal_week_end_d": [_date(2026, 5, 16)] * 5,
            "channel_originated": ["store", "store", "online", "online", "online"],
            "channel_fulfilled": ["store", "store", "online", "store", "store"],
            "fulfillment_type": ["pickup", "ship", "ship", "pickup", "pickup"],
            "fulfillment_subtype": ["std", "std", "exp", "std", "std"],
            "gross_margin": [0.30, 0.31, 0.28, 0.29, 0.27],
        }
    )
    cols = derive_duckdb_schema(df)
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.ensure_data_table("gross_margin", cols)
        wh.upsert_dataframe("gross_margin", df, primary_key=pk)
        # Contract: every row is uniquely identified by the 8-col NK.
        _, ck = wh.execute_sql(
            "SELECT COUNT(*), COUNT(DISTINCT (tcin, location_id, "
            "location_id_originated, fiscal_week_end_d, channel_originated, "
            "channel_fulfilled, fulfillment_type, fulfillment_subtype)) "
            "FROM gross_margin"
        )
        assert ck[0][0] == ck[0][1] == 5

        # Counter-test: a 7-col key (DROPPING location_id_originated) would
        # collapse 5 rows into 4 — that's the 3,831-row data-loss scenario.
        _, narrow = wh.execute_sql(
            "SELECT COUNT(DISTINCT (tcin, location_id, fiscal_week_end_d, "
            "channel_originated, channel_fulfilled, fulfillment_type, "
            "fulfillment_subtype)) FROM gross_margin"
        )
        assert narrow[0][0] == 4, (
            "fixture must demonstrate that the 7-col key is too narrow; "
            "the row with location_id_originated=5678 collapses into the "
            "1234-origin row, hiding 1 row of data"
        )

        # Re-load idempotency — count unchanged.
        wh.upsert_dataframe("gross_margin", df, primary_key=pk)
        _, ck2 = wh.execute_sql("SELECT COUNT(*) FROM gross_margin")
        assert ck2[0][0] == 5
    finally:
        wh.close()


def test_gross_margin_item_natural_key_idempotency(tmp_path: Path) -> None:
    """Patch #7. Natural PK is 6 cols (no location dimensions — this is the
    item rollup). Verified empirically: 617 = 617.
    """
    from datetime import date as _date

    pk = (
        "tcin", "fiscal_week_end_d",
        "channel_originated", "channel_fulfilled",
        "fulfillment_type", "fulfillment_subtype",
    )
    df = pl.DataFrame(
        {
            "tcin": [100, 100, 100, 100],
            "fiscal_week_end_d": [_date(2026, 5, 16)] * 4,
            "channel_originated": ["store", "store", "online", "online"],
            "channel_fulfilled": ["store", "store", "online", "store"],
            "fulfillment_type": ["pickup", "ship", "ship", "pickup"],
            "fulfillment_subtype": ["std", "std", "exp", "std"],
            "gross_margin": [0.30, 0.31, 0.28, 0.29],
        }
    )
    cols = derive_duckdb_schema(df)
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.ensure_data_table("gross_margin_item", cols)
        wh.upsert_dataframe("gross_margin_item", df, primary_key=pk)
        _, ck = wh.execute_sql(
            "SELECT COUNT(*), COUNT(DISTINCT (tcin, fiscal_week_end_d, "
            "channel_originated, channel_fulfilled, fulfillment_type, "
            "fulfillment_subtype)) FROM gross_margin_item"
        )
        assert ck[0][0] == ck[0][1] == 4

        # Counter-test: 2-col (tcin, fiscal_week_end_d) would collapse to 1
        # row — the pre-#7 broken state.
        _, narrow = wh.execute_sql(
            "SELECT COUNT(DISTINCT (tcin, fiscal_week_end_d)) "
            "FROM gross_margin_item"
        )
        assert narrow[0][0] == 1

        wh.upsert_dataframe("gross_margin_item", df, primary_key=pk)
        _, ck2 = wh.execute_sql("SELECT COUNT(*) FROM gross_margin_item")
        assert ck2[0][0] == 4
    finally:
        wh.close()


# ---------- Patch #8: HISTORY backfill merges into existing weekly tables ----------


def _load_via_sync_path(wh, dataset: str, zip_path):
    """Mimic sync._process_one_file's parse(+rename)→pick-PK→upsert sequence."""
    from bpd_mcp.parsers import classify_filename, derive_duckdb_schema, read_dataframe
    from bpd_mcp.sync import _pick_primary_key

    parsed = classify_filename(zip_path.name)
    assert parsed is not None and parsed.pattern.dataset == dataset
    result = read_dataframe(zip_path, dataset)
    assert result.method == "strict", result.primary_error
    df = result.df
    pk = _pick_primary_key(parsed, df.columns)
    assert all(c in df.columns for c in pk), (pk, df.columns)
    wh.ensure_data_table(dataset, derive_duckdb_schema(df))
    wh.upsert_dataframe(
        dataset, df, primary_key=pk, replace_scope=parsed.pattern.replace_scope
    )
    return df, pk


def test_history_inv_weekly_merges_into_existing_table(tmp_path: Path) -> None:
    """HISTORY_INV_WEEKLY ships `WEEK_END_D` where the current feed ships
    `BUSINESS_D`. The canonical shim renames at ingest so the backfill lands
    in the same table/column as the regular feed, keyed identically."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # Regular current-feed file (business_d) creates the table.
        regular = _zip(
            tmp_path / "BV_139440_WEEKLY_INV_TCIN_LOC_06062026_KW.zip",
            "BUSINESS_D\tPRIMARY_VENDOR_ID\tTCIN\tDPCI\tLOCATION_ID\t"
            "BEGINNING_ON_HAND_Q\tENDING_ON_HAND_Q\tENDING_ON_TRANSFER_Q\n"
            "2026-06-06\t2003081\t89854821\t003-02-1080\t1442\t25\t20\t2\n",
        )
        _load_via_sync_path(wh, "inventory_weekly", regular)

        # HISTORY file (week_end_d) for an older week merges in.
        history = _zip(
            tmp_path / "BV_139440_HISTORY_INV_WEEKLY_01112025_KW.zip",
            "WEEK_END_D\tPRIMARY_VENDOR_ID\tTCIN\tDPCI\tLOCATION_ID\t"
            "BEGINNING_ON_HAND_Q\tENDING_ON_HAND_Q\tENDING_ON_TRANSFER_Q\n"
            "2025-01-11\t2003081\t89854821\t003-02-1080\t1442\t5\t2\t0\n",
        )
        df, pk = _load_via_sync_path(wh, "inventory_weekly", history)
        assert pk == ("tcin", "location_id", "business_d")

        _, rows = wh.execute_sql(
            "SELECT CAST(business_d AS DATE), SUM(ending_on_hand_q) "
            "FROM inventory_weekly GROUP BY 1 ORDER BY 1"
        )
        assert len(rows) == 2, "history week + current week must coexist"

        # Re-load the history file: idempotent, no duplication.
        wh.upsert_dataframe("inventory_weekly", df, primary_key=pk)
        _, total = wh.execute_sql("SELECT COUNT(*) FROM inventory_weekly")
        assert total[0][0] == 2
    finally:
        wh.close()


def test_history_gm_weekly_merges_into_existing_table(tmp_path: Path) -> None:
    """HISTORY_GM_WEEKLY ships `FISCAL_WEEK_END_DATE` where the current feed
    ships `FISCAL_WEEK_END_D`; same 8-col natural key after the rename."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    gm_cols = (
        "VENDOR_ID\tTCIN\tDPCI\tCHANNEL_ORIGINATED\tLOCATION_ID_ORIGINATED\t"
        "LOCATION_ID\tCHANNEL_FULFILLED\tFULFILLMENT_TYPE\tFULFILLMENT_SUBTYPE\t"
        "NET_SALES_A\tNET_SALES_Q\tADJUSTED_GROSS_MARGIN_A"
    )
    gm_vals = "2003081\t89854823\t003-02-1327\tSTORE\t2036\t2036\tSTORE\tNA\tNA\t19.99\t1.0\t8.5"
    try:
        regular = _zip(
            tmp_path / "BV_139440_WEEKLY_GM_TCIN_LOC_06062026_KW.zip",
            f"FISCAL_WEEK_END_D\t{gm_cols}\n2026-06-06\t{gm_vals}\n",
        )
        _load_via_sync_path(wh, "gross_margin", regular)

        history = _zip(
            tmp_path / "BV_139440_HISTORY_GM_WEEKLY_01182025_KW.zip",
            f"FISCAL_WEEK_END_DATE\t{gm_cols}\n2025-01-18\t{gm_vals}\n",
        )
        df, pk = _load_via_sync_path(wh, "gross_margin", history)
        assert pk == (
            "tcin", "location_id", "location_id_originated", "fiscal_week_end_d",
            "channel_originated", "channel_fulfilled",
            "fulfillment_type", "fulfillment_subtype",
        )

        _, rows = wh.execute_sql(
            "SELECT COUNT(DISTINCT fiscal_week_end_d), COUNT(*) FROM gross_margin"
        )
        assert rows[0] == (2, 2)

        wh.upsert_dataframe("gross_margin", df, primary_key=pk)
        _, total = wh.execute_sql("SELECT COUNT(*) FROM gross_margin")
        assert total[0][0] == 2
    finally:
        wh.close()


def test_history_sales_weekly_merges_and_overlap_replaces(tmp_path: Path) -> None:
    """HISTORY_SALES_WEEKLY uses the SAME header as the current feed (no shim
    needed). An overlapping week must REPLACE, not duplicate."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    hdr = (
        "SALES_DATE\tVENDOR_ID\tBARCODE\tTCIN\tDPCI\tORIGINATION_CHANNEL\t"
        "REPORTING_CHANNEL\tFULFILLMENT_TYPE\tLOCATION_ID\tSALE_AMOUNT\tSALE_QUANTITY"
    )
    try:
        regular = _zip(
            tmp_path / "BV_139440_WEEKLY_SALES_TCIN_LOC_03282026_KW.zip",
            f"{hdr}\n"
            "2026-03-28\t2003081\t850036134121\t89854823\t003-02-1327\tSTORE\tSTR\tCARRYOUT\t918\t19.99\t1.0\n",
        )
        _load_via_sync_path(wh, "sales_weekly", regular)

        # HISTORY file covers BOTH an older week and the same 3/28 week
        # (revised amount) for the same natural key.
        history = _zip(
            tmp_path / "BV_139440_HISTORY_SALES_WEEKLY_03282026_KW.zip",
            f"{hdr}\n"
            "2026-03-28\t2003081\t850036134121\t89854823\t003-02-1327\tSTORE\tSTR\tCARRYOUT\t918\t18.99\t1.0\n"
            "2025-01-11\t2003081\t850036134121\t89854823\t003-02-1327\tSTORE\tSTR\tCARRYOUT\t918\t19.99\t1.0\n",
        )
        _df, pk = _load_via_sync_path(wh, "sales_weekly", history)
        assert pk == ("tcin", "location_id", "sales_date")

        _, rows = wh.execute_sql(
            "SELECT COUNT(*), SUM(sale_amount) FROM sales_weekly"
        )
        # 2 rows (one per week) — the 3/28 overlap was replaced (18.99), not
        # appended: total = 18.99 + 19.99.
        assert rows[0][0] == 2
        assert abs(rows[0][1] - 38.98) < 0.01
    finally:
        wh.close()


# ---------- Patch #8 review fixes: atomicity, week-scoped replace, widening ----------


def test_upsert_rolls_back_delete_when_insert_fails(tmp_path: Path) -> None:
    """CRITICAL regression guard (adversarial review): DELETE+INSERT must be
    atomic. If the INSERT fails (e.g. an uncastable value in an incoming
    column), the deleted rows must be RESTORED — otherwise one malformed
    backfill file permanently destroys previously-loaded weeks while the
    ledger still says the original file is 'loaded'."""
    import pytest

    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "business_d")
    good = pl.DataFrame(
        {
            "tcin": [100],
            "location_id": [1234],
            "business_d": ["2025-01-11"],
            "ending_on_hand_q": [25],
        }
    )
    try:
        wh.ensure_data_table("inventory_weekly", derive_duckdb_schema(good))
        wh.upsert_dataframe(
            "inventory_weekly", good, primary_key=pk, replace_scope=("business_d",)
        )

        # Re-ship of the same week where the on-hand column parsed as String
        # because one cell is 'N/A' — uncastable to BIGINT at INSERT time.
        bad = pl.DataFrame(
            {
                "tcin": [100],
                "location_id": [1234],
                "business_d": ["2025-01-11"],
                "ending_on_hand_q": ["N/A"],
            }
        )
        import duckdb

        with pytest.raises(duckdb.Error):
            wh.upsert_dataframe(
                "inventory_weekly", bad, primary_key=pk, replace_scope=("business_d",)
            )

        # The original row must still be there.
        _, rows = wh.execute_sql(
            "SELECT COUNT(*), SUM(ending_on_hand_q) FROM inventory_weekly"
        )
        assert rows[0] == (1, 25), "failed INSERT must roll back its DELETE"
    finally:
        wh.close()


def test_week_scoped_replace_prevents_mixed_generation_chimera(tmp_path: Path) -> None:
    """MAJOR regression guard (adversarial review): a weekly file is the
    complete extract of its week. When a rebuilt generation (HISTORY) carries
    a different key-set than the originally loaded file for the same week,
    week-scoped deletion must replace the WHOLE week — per-key deletion would
    leave a mixed-generation week whose totals match neither source."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "sales_date")
    scope = ("sales_date",)
    regular = pl.DataFrame(
        {
            "tcin": [89854823, 89854823],
            "location_id": [918, 777],
            "sales_date": ["2026-03-28", "2026-03-28"],
            "sale_amount": [19.99, 5.00],
            "sale_quantity": [1, 1],
        }
    )
    # HISTORY rebuild of the same week carries only ONE of the two keys.
    history = pl.DataFrame(
        {
            "tcin": [89854823],
            "location_id": [918],
            "sales_date": ["2026-03-28"],
            "sale_amount": [18.99],
            "sale_quantity": [1],
        }
    )
    try:
        wh.ensure_data_table("sales_weekly", derive_duckdb_schema(regular))
        wh.upsert_dataframe(
            "sales_weekly", regular, primary_key=pk, replace_scope=scope
        )
        wh.upsert_dataframe(
            "sales_weekly", history, primary_key=pk, replace_scope=scope
        )
        _, rows = wh.execute_sql(
            "SELECT COUNT(*), SUM(sale_amount) FROM sales_weekly"
        )
        # The whole week is the HISTORY generation: 1 row, $18.99 — NOT a
        # chimera of 2 rows totaling $23.99.
        assert rows[0][0] == 1
        assert abs(rows[0][1] - 18.99) < 0.01
    finally:
        wh.close()


def test_week_scoped_replace_leaves_other_weeks_alone(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "sales_date")
    scope = ("sales_date",)
    df_a = pl.DataFrame(
        {
            "tcin": [1],
            "location_id": [10],
            "sales_date": ["2026-03-21"],
            "sale_amount": [10.0],
        }
    )
    df_b = pl.DataFrame(
        {
            "tcin": [2],
            "location_id": [20],
            "sales_date": ["2026-03-28"],
            "sale_amount": [20.0],
        }
    )
    try:
        wh.ensure_data_table("sales_weekly", derive_duckdb_schema(df_a))
        wh.upsert_dataframe("sales_weekly", df_a, primary_key=pk, replace_scope=scope)
        wh.upsert_dataframe("sales_weekly", df_b, primary_key=pk, replace_scope=scope)
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows[0][0] == 2, "loading week B must not touch week A"
    finally:
        wh.close()


def test_upsert_rejects_null_replace_scope_values(tmp_path: Path) -> None:
    """A NULL week-end date could never be matched by a future scoped DELETE —
    loading it would duplicate on every re-load. Must fail loudly, atomically."""
    import pytest

    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "sales_date")
    good = pl.DataFrame(
        {"tcin": [1], "location_id": [10], "sales_date": ["2026-03-21"], "sale_amount": [1.0]}
    )
    bad = pl.DataFrame(
        {"tcin": [2], "location_id": [20], "sales_date": [None], "sale_amount": [2.0]}
    ).with_columns(pl.col("sales_date").cast(pl.String))
    try:
        wh.ensure_data_table("sales_weekly", derive_duckdb_schema(good))
        wh.upsert_dataframe("sales_weekly", good, primary_key=pk, replace_scope=("sales_date",))
        with pytest.raises(RuntimeError, match="null_replace_scope_values"):
            wh.upsert_dataframe(
                "sales_weekly", bad, primary_key=pk, replace_scope=("sales_date",)
            )
        _, rows = wh.execute_sql("SELECT COUNT(*) FROM sales_weekly")
        assert rows[0][0] == 1, "rejected load must leave the table untouched"
    finally:
        wh.close()


def test_ensure_data_table_widens_for_new_columns(tmp_path: Path) -> None:
    """Fresh-rebuild race fix (adversarial review): a generation carrying
    extra columns must widen the table (ALTER ADD COLUMN) so its values land,
    regardless of which generation created the table first."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    pk = ("tcin", "location_id", "business_d")
    narrow = pl.DataFrame(
        {
            "tcin": [1],
            "location_id": [10],
            "business_d": ["2026-06-06"],
            "ending_on_hand_q": [5],
        }
    )
    wide = pl.DataFrame(
        {
            "tcin": [2],
            "location_id": [20],
            "business_d": ["2025-01-11"],
            "ending_on_hand_q": [7],
            "ending_on_transfer_q": [3],
        }
    )
    try:
        wh.ensure_data_table("inventory_weekly", derive_duckdb_schema(narrow))
        wh.upsert_dataframe(
            "inventory_weekly", narrow, primary_key=pk, replace_scope=("business_d",)
        )
        # Second generation carries an extra column: table must widen.
        wh.ensure_data_table("inventory_weekly", derive_duckdb_schema(wide))
        wh.upsert_dataframe(
            "inventory_weekly", wide, primary_key=pk, replace_scope=("business_d",)
        )
        _, rows = wh.execute_sql(
            "SELECT ending_on_transfer_q FROM inventory_weekly "
            "ORDER BY CAST(business_d AS DATE)"
        )
        assert rows == [(3,), (None,)], "new column lands; old rows get NULL"
    finally:
        wh.close()


def test_list_datasets_dedupes_multi_pattern_datasets(tmp_path: Path) -> None:
    """HISTORY patterns map a second FilePattern to the same dataset —
    list_datasets must still report each dataset exactly once."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    df = pl.DataFrame(
        {"tcin": [1], "location_id": [10], "sales_date": ["2026-03-21"], "sale_amount": [1.0]}
    )
    try:
        wh.ensure_data_table("sales_weekly", derive_duckdb_schema(df))
        wh.upsert_dataframe(
            "sales_weekly", df, primary_key=("tcin", "location_id", "sales_date")
        )
        listed = [d["dataset"] for d in wh.list_datasets()]
        assert listed.count("sales_weekly") == 1, listed
    finally:
        wh.close()


def test_history_gm_same_week_cross_generation_replace(tmp_path: Path) -> None:
    """Test-coverage fix (adversarial review): the overlap-REPLACE claim must
    hold where the shim actually operates — a HISTORY GM file re-shipping a
    week already loaded from the REGULAR feed replaces it wholesale."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    gm_cols = (
        "VENDOR_ID\tTCIN\tDPCI\tCHANNEL_ORIGINATED\tLOCATION_ID_ORIGINATED\t"
        "LOCATION_ID\tCHANNEL_FULFILLED\tFULFILLMENT_TYPE\tFULFILLMENT_SUBTYPE\t"
        "NET_SALES_A\tNET_SALES_Q\tADJUSTED_GROSS_MARGIN_A"
    )
    try:
        regular = _zip(
            tmp_path / "BV_139440_WEEKLY_GM_TCIN_LOC_01182025_KW.zip",
            f"FISCAL_WEEK_END_D\t{gm_cols}\n"
            "2025-01-18\t2003081\t89854823\t003-02-1327\tSTORE\t2036\t2036\tSTORE\tNA\tNA\t19.99\t1.0\t8.5\n"
            "2025-01-18\t2003081\t89854823\t003-02-1327\tONLINE\t912\t912\tSTORE\tSHIPTOHOME\tNA\t7.00\t1.0\t2.0\n",
        )
        _load_via_sync_path(wh, "gross_margin", regular)

        # HISTORY rebuild of the SAME week: different key-set (one row only),
        # different date column name, revised value.
        history = _zip(
            tmp_path / "BV_139440_HISTORY_GM_WEEKLY_01182025_KW.zip",
            f"FISCAL_WEEK_END_DATE\t{gm_cols}\n"
            "2025-01-18\t2003081\t89854823\t003-02-1327\tSTORE\t2036\t2036\tSTORE\tNA\tNA\t18.50\t1.0\t8.0\n",
        )
        _load_via_sync_path(wh, "gross_margin", history)

        _, rows = wh.execute_sql(
            "SELECT COUNT(*), SUM(net_sales_a) FROM gross_margin"
        )
        assert rows[0][0] == 1, "HISTORY generation replaces the whole week"
        assert abs(rows[0][1] - 18.50) < 0.01
    finally:
        wh.close()


def test_history_inv_same_week_cross_generation_replace(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    hdr = (
        "PRIMARY_VENDOR_ID\tTCIN\tDPCI\tLOCATION_ID\t"
        "BEGINNING_ON_HAND_Q\tENDING_ON_HAND_Q"
    )
    try:
        regular = _zip(
            tmp_path / "BV_139440_WEEKLY_INV_TCIN_LOC_01112025_KW.zip",
            f"BUSINESS_D\t{hdr}\n"
            "2025-01-11\t2003081\t89854821\t003-02-1080\t1442\t25\t20\n"
            "2025-01-11\t2003081\t89854821\t003-02-1080\t999\t5\t5\n",
        )
        _load_via_sync_path(wh, "inventory_weekly", regular)

        history = _zip(
            tmp_path / "BV_139440_HISTORY_INV_WEEKLY_01112025_KW.zip",
            f"WEEK_END_D\t{hdr}\n"
            "2025-01-11\t2003081\t89854821\t003-02-1080\t1442\t25\t22\n",
        )
        _load_via_sync_path(wh, "inventory_weekly", history)

        _, rows = wh.execute_sql(
            "SELECT COUNT(*), SUM(ending_on_hand_q) FROM inventory_weekly"
        )
        assert rows[0] == (1, 22), "HISTORY generation replaces the whole week"
    finally:
        wh.close()
