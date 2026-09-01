"""`BigQueryWarehouse.list_datasets()` — the row assembly and the active/retired rule.

Gate note on why this file exists: every other test of the listing surface feeds
canned rows *into* the tools (`tests/test_tools_admin.py`'s `FakeWarehouse`), so
the derivation that builds those rows — new in the BigQuery swap, and the only
place `status` is now computed — had no test at any tier. `status` used to be
read off `parsers.FilePattern.retired`; that module is deleted, and the
replacement infers it from how stale the newest file feeding the table is.

Everything here is pure python and free. `list_datasets()` reads exactly three
things — `_base_row_counts()` (0 bytes), `_ingest_rollup()` (~10 MB) and
`_date_ranges()` (~527 MB) — each behind a `_TTLCache`. Seeding those three
caches exercises the real method bodies and the real derivation while the client
is never touched: the warehouses below are built with `client=_NoClient()`,
which raises on any attribute access, so a query would be an error rather than a
bill.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from bpd_mcp.bq import _RETIRED_AFTER_DAYS, BigQueryWarehouse, LogicalTable

P = "biom-reporting-s26"


class _NoClient:
    """Any attribute access is a bug: nothing here may reach BigQuery."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - failure path
        raise AssertionError(f"list_datasets() touched the BigQuery client: .{name}")


def _table(name: str, *, patterns: tuple[str, ...] = (), base: str | None = None) -> LogicalTable:
    return LogicalTable(
        name=name,
        sql=f"SELECT 1 AS x FROM `{base or f'{P}.bpd_raw.{name}'}`",
        base_tables=(base or f"{P}.bpd_raw.{name}",),
        date_column="business_d",
        patterns=patterns,
    )


def _warehouse(
    registry: dict[str, LogicalTable],
    *,
    counts: dict[str, int] | None = None,
    ingest: dict[str, dict[str, Any]] | None = None,
    ranges: dict[str, dict[str, Any]] | None = None,
) -> BigQueryWarehouse:
    wh = BigQueryWarehouse(client=_NoClient(), registry=registry)
    wh._rowcounts.put(counts or {})
    wh._ingest.put(ingest or {})
    wh._dateranges.put(ranges or {})
    return wh


def _ingest_row(
    pattern: str, *, files: int, file_date: dt.date, lag: int, downloaded: dt.datetime
) -> dict[str, Any]:
    """One `_ingest_rollup()` entry, with only the keys list_datasets reads."""
    return {
        "pattern": pattern,
        "files": files,
        "max_file_date": file_date,
        "min_file_date": file_date,
        "max_downloaded_at": downloaded,
        "total_bytes": 1,
        "lag_days": lag,
    }


def _row(wh: BigQueryWarehouse, name: str) -> dict[str, Any]:
    return {r["dataset"]: r for r in wh.list_datasets()}[name]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_row_keys_are_exactly_the_documented_contract() -> None:
    """`bpd_list_datasets` and `bpd_data_freshness` read these by name.

    Dropping or renaming one is a silent KeyError in a tool, so the key set is
    pinned exactly rather than checked for a superset.
    """
    wh = _warehouse({"sales_daily": _table("sales_daily")})
    (row,) = wh.list_datasets()
    assert set(row) == {
        "dataset",
        "feed_kind",
        "status",
        "row_count",
        "date_column",
        "min_date",
        "max_date",
        "content_column",
        "content_min_date",
        "content_max_date",
        "file_count",
        "last_loaded_at",
    }


def test_one_row_per_logical_table_even_when_several_patterns_feed_it() -> None:
    """The old `dict.fromkeys` dedupe is gone; the invariant it protected is not.

    Two ingestion patterns feeding one logical table must collapse to one row,
    with the file counts summed and the newest download winning.
    """
    reg = {"sales_weekly": _table("sales_weekly", patterns=("WEEKLY_SALES", "HISTORY_WEEKLY"))}
    wh = _warehouse(
        reg,
        ingest={
            "WEEKLY_SALES": _ingest_row(
                "WEEKLY_SALES",
                files=40,
                file_date=dt.date(2026, 8, 29),
                lag=3,
                downloaded=dt.datetime(2026, 9, 1, 6, 49),
            ),
            "HISTORY_WEEKLY": _ingest_row(
                "HISTORY_WEEKLY",
                files=2,
                file_date=dt.date(2025, 6, 1),
                lag=457,
                downloaded=dt.datetime(2025, 6, 2, 6, 49),
            ),
        },
    )

    rows = wh.list_datasets()
    assert [r["dataset"] for r in rows] == ["sales_weekly"]
    assert rows[0]["file_count"] == 42
    assert rows[0]["last_loaded_at"] == dt.datetime(2026, 9, 1, 6, 49)


def test_row_count_is_the_primary_base_table_and_missing_counts_are_zero() -> None:
    """`row_count` follows `primary_base_table` — the FIRST base table, not any match."""
    joined = LogicalTable(
        name="inventory_daily",
        sql="SELECT 1 AS x",
        base_tables=(f"{P}.bpd_raw.daily_inventory", f"{P}.biom_canvas.dim_item"),
        date_column="business_d",
    )
    wh = _warehouse(
        {"inventory_daily": joined, "gross_margin": _table("gross_margin")},
        counts={
            f"{P}.bpd_raw.daily_inventory": 1_234_567,
            f"{P}.biom_canvas.dim_item": 99,
        },
    )
    assert _row(wh, "inventory_daily")["row_count"] == 1_234_567
    # Not in __TABLES__ at all -> 0, never None: the tools format this number.
    assert _row(wh, "gross_margin")["row_count"] == 0


# ---------------------------------------------------------------------------
# status: derived from feed lag, not from a stored flag
# ---------------------------------------------------------------------------


def test_retired_threshold_is_ninety_days() -> None:
    """Pins the constant the two tests below lean on, so they cannot drift silently."""
    assert _RETIRED_AFTER_DAYS == 90


@pytest.mark.parametrize(
    ("lag", "expected"),
    [
        (0, "active"),
        (3, "active"),
        (_RETIRED_AFTER_DAYS, "active"),  # boundary: exactly 90 days is NOT retired
        (_RETIRED_AFTER_DAYS + 1, "retired"),
        (108, "retired"),  # the real *_TCIN rollups on 2026-09-01
    ],
)
def test_status_follows_the_newest_file_lag(lag: int, expected: str) -> None:
    reg = {"sales_weekly_item": _table("sales_weekly_item", patterns=("WEEKLY_SALES_TCIN",))}
    wh = _warehouse(
        reg,
        ingest={
            "WEEKLY_SALES_TCIN": _ingest_row(
                "WEEKLY_SALES_TCIN",
                files=12,
                file_date=dt.date(2026, 5, 16),
                lag=lag,
                downloaded=dt.datetime(2026, 5, 16, 6, 49),
            )
        },
    )
    assert _row(wh, "sales_weekly_item")["status"] == expected


def test_a_live_pattern_outranks_its_retired_history_twin() -> None:
    """The case the docstring calls out: sales_weekly stays ACTIVE.

    Its live weekly feed is 3 days old; the HISTORY twin that also feeds it last
    landed over a year ago. Status must follow the NEWEST file_date, so a
    long-dead backfill pattern cannot retire a table that is still loading.
    """
    reg = {"sales_weekly": _table("sales_weekly", patterns=("WEEKLY_SALES", "HISTORY_WEEKLY"))}
    wh = _warehouse(
        reg,
        ingest={
            "WEEKLY_SALES": _ingest_row(
                "WEEKLY_SALES",
                files=40,
                file_date=dt.date(2026, 8, 29),
                lag=3,
                downloaded=dt.datetime(2026, 9, 1, 6, 49),
            ),
            "HISTORY_WEEKLY": _ingest_row(
                "HISTORY_WEEKLY",
                files=2,
                file_date=dt.date(2025, 6, 1),
                lag=457,
                downloaded=dt.datetime(2025, 6, 2, 6, 49),
            ),
        },
    )
    assert _row(wh, "sales_weekly")["status"] == "active"


def test_both_patterns_dead_retires_the_table() -> None:
    """The inverse of the case above — the rule is not just 'two patterns means active'."""
    reg = {"sales_weekly": _table("sales_weekly", patterns=("WEEKLY_SALES", "HISTORY_WEEKLY"))}
    wh = _warehouse(
        reg,
        ingest={
            "WEEKLY_SALES": _ingest_row(
                "WEEKLY_SALES",
                files=40,
                file_date=dt.date(2026, 1, 5),
                lag=239,
                downloaded=dt.datetime(2026, 1, 5, 6, 49),
            ),
            "HISTORY_WEEKLY": _ingest_row(
                "HISTORY_WEEKLY",
                files=2,
                file_date=dt.date(2025, 6, 1),
                lag=457,
                downloaded=dt.datetime(2025, 6, 2, 6, 49),
            ),
        },
    )
    assert _row(wh, "sales_weekly")["status"] == "retired"


def test_a_tie_on_the_newest_file_date_takes_the_smaller_lag() -> None:
    """Two patterns landing the same file_date must not produce a coin flip.

    `lag_days` is computed per pattern against CURRENT_DATE(), so a tie should
    agree — but the reduction takes `min(...)` deliberately, and a max would
    retire a table that is current. Skewed lags on an identical date pin which.
    """
    reg = {"po_plan_daily": _table("po_plan_daily", patterns=("PO_PLAN", "PO_PLAN_ALT"))}
    wh = _warehouse(
        reg,
        ingest={
            "PO_PLAN": _ingest_row(
                "PO_PLAN",
                files=5,
                file_date=dt.date(2026, 8, 30),
                lag=200,
                downloaded=dt.datetime(2026, 8, 30, 6, 49),
            ),
            "PO_PLAN_ALT": _ingest_row(
                "PO_PLAN_ALT",
                files=5,
                file_date=dt.date(2026, 8, 30),
                lag=2,
                downloaded=dt.datetime(2026, 8, 30, 6, 49),
            ),
        },
    )
    assert _row(wh, "po_plan_daily")["status"] == "active"


def test_a_table_with_no_file_feed_is_active_with_no_files() -> None:
    """`item_attr`-shaped tables declare no patterns at all.

    Absence of a ledger entry is not staleness: the table is a dimension that
    the pipeline never files. Reporting it retired would mark half the registry
    dead in `bpd_list_datasets`.
    """
    wh = _warehouse({"item_attr": _table("item_attr")})
    row = _row(wh, "item_attr")
    assert row["status"] == "active"
    assert row["file_count"] == 0
    assert row["last_loaded_at"] is None


def test_a_declared_pattern_missing_from_the_ledger_does_not_crash_or_retire() -> None:
    """A newly declared pattern the pipeline has not filed yet has no ledger row."""
    reg = {"forecast_weekly": _table("forecast_weekly", patterns=("DFE_FORECAST",))}
    wh = _warehouse(reg, ingest={"SOMETHING_ELSE": _ingest_row(
        "SOMETHING_ELSE",
        files=1,
        file_date=dt.date(2020, 1, 1),
        lag=2400,
        downloaded=dt.datetime(2020, 1, 1),
    )})
    row = _row(wh, "forecast_weekly")
    assert row["status"] == "active"
    assert row["file_count"] == 0
    assert row["last_loaded_at"] is None


# ---------------------------------------------------------------------------
# dates: the snapshot / content split, and the fallback
# ---------------------------------------------------------------------------


def test_snapshot_and_content_ranges_are_reported_separately() -> None:
    """forecast_weekly is the reason both pairs exist.

    Its snapshot column says when the forecast was cut; its content column says
    what future week the row is about. Collapsing them would report a forward
    forecast as ending months ago.
    """
    reg = {"forecast_weekly": _table("forecast_weekly", patterns=("DFE",))}
    wh = _warehouse(
        reg,
        ranges={
            "forecast_weekly": {
                "date_column": "last_update_d",
                "content_column": "fiscal_week_begin_d",
                "min_date": dt.date(2026, 4, 6),
                "max_date": dt.date(2026, 7, 20),
                "content_min_date": dt.date(2026, 4, 6),
                "content_max_date": dt.date(2026, 10, 11),
            }
        },
    )
    row = _row(wh, "forecast_weekly")
    assert (row["date_column"], row["max_date"]) == ("last_update_d", dt.date(2026, 7, 20))
    assert (row["content_column"], row["content_max_date"]) == (
        "fiscal_week_begin_d",
        dt.date(2026, 10, 11),
    )


def test_dates_fall_back_to_the_declaration_when_the_sweep_returned_no_row() -> None:
    """An empty base table produces no sweep row; the column is still known.

    `date_column` must still name the declared column rather than going None,
    because `bpd_data_freshness` prints it as "the column we measured".
    """
    wh = _warehouse({"sales_daily": _table("sales_daily")}, ranges={})
    row = _row(wh, "sales_daily")
    assert row["date_column"] == "business_d"
    assert row["content_column"] == "business_d"
    assert row["min_date"] is None
    assert row["max_date"] is None
    assert row["content_min_date"] is None
    assert row["content_max_date"] is None


def test_a_null_extent_does_not_fall_back_to_the_declared_column_name() -> None:
    """A sweep row with NULL extents (all-NULL/uncastable dates) keeps its own column.

    The fallback is `or entry.date_column` on the COLUMN, not on the dates, so a
    table whose dates are all NULL must report the column the sweep actually
    measured and nulls for the extents.
    """
    reg = {"location_attr": _table("location_attr")}
    wh = _warehouse(
        reg,
        ranges={
            "location_attr": {
                "date_column": "last_remodel_date",
                "content_column": "last_remodel_date",
                "min_date": None,
                "max_date": None,
                "content_min_date": None,
                "content_max_date": None,
            }
        },
    )
    row = _row(wh, "location_attr")
    assert row["date_column"] == "last_remodel_date"
    assert row["max_date"] is None


def test_feed_kind_comes_from_the_shared_map_and_unknown_is_explicit() -> None:
    """A table absent from FEED_KINDS reports 'unknown', not None or KeyError."""
    from bpd_mcp.column_roles import FEED_KINDS

    wh = _warehouse({"sales_daily": _table("sales_daily"), "made_up": _table("made_up")})
    assert _row(wh, "sales_daily")["feed_kind"] == FEED_KINDS["sales_daily"]
    assert _row(wh, "made_up")["feed_kind"] == "unknown"
