"""Tool-surface tests for tools/query.py: bpd_run_sql, bpd_describe_schema, and
the arithmetic / filter / format behaviour of the sales and inventory tools.

TIER. Everything that has to produce a NUMBER runs on the real BigQuery engine
against literal fixture CTEs (`@pytest.mark.bq`, 0 bytes billed) — see the long
note in conftest.py on why there is no local engine double. The handful of
checks that provably return before any query is issued (SQL safety, the
read-only gate, "no such logical table") stay in the default tier and assert
that no client call happened at all.

The column names in the fixtures are the ones Target actually ships
(`sales_date`, `sale_quantity`, `sale_amount`, `business_d`,
`ending_on_hand_q`), and conftest's `_TYPES` map pins them to the PRODUCTION
BigQuery types — notably `sale_quantity` is FLOAT64, so every units total below
is a float. Role-resolution coverage proper lives in
tests/test_analytics_real_columns.py; this file is about the tool surface.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from bpd_mcp.bq import BigQueryWarehouse
from bpd_mcp.schemas import (
    DescribeSchemaInput,
    InventorySnapshotInput,
    RunSqlInput,
    SalesSummaryInput,
    TopSkusInput,
)
from bpd_mcp.tools.query import (
    describe_schema,
    get_inventory_snapshot,
    get_sales_summary,
    get_top_skus,
    run_sql,
)

# ---------------------------------------------------------------------------
# Offline scaffolding — for the paths that must return BEFORE any query runs.
# ---------------------------------------------------------------------------


class _NeverQueried:
    """Stands in for a `bigquery.Client` that the code under test must not touch.

    Any attribute access at all (`.query`, `.close`, ...) fails the test, which
    is what makes "this path costs nothing" an assertion rather than a claim.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - only on failure
        raise AssertionError(f"BigQuery client was touched (.{name}) in an offline test")


def _offline_warehouse(registry: dict[str, Any] | None = None) -> BigQueryWarehouse:
    return BigQueryWarehouse(client=_NeverQueried(), registry=registry or {})


class _WritableWarehouse:
    """The one thing a real BigQueryWarehouse cannot be: `read_only` False."""

    read_only = False


# ---------------------------------------------------------------------------
# Fixture rows. Every expected number below is hand-computable from these.
# ---------------------------------------------------------------------------

# Saturdays, i.e. Target's fiscal week-END anchor.
SALES_WEEKLY_ROWS = [
    {"sales_date": "2026-05-02", "tcin": 100, "location_id": 2750,
     "sale_quantity": 50, "sale_amount": 150.0},
    {"sales_date": "2026-05-02", "tcin": 100, "location_id": 3275,
     "sale_quantity": 30, "sale_amount": 90.0},
    {"sales_date": "2026-05-02", "tcin": 200, "location_id": 2750,
     "sale_quantity": 20, "sale_amount": 80.0},
    {"sales_date": "2026-05-09", "tcin": 100, "location_id": 2750,
     "sale_quantity": 40, "sale_amount": 120.0},
    {"sales_date": "2026-05-09", "tcin": 200, "location_id": 2750,
     "sale_quantity": 10, "sale_amount": 40.0},
]
# week of 2026-05-02: 50 + 30 + 20 = 100 units, 320.00
# week of 2026-05-09: 40 + 10      =  50 units, 160.00
# tcin 100 total: 120 units / 360.00 ; tcin 200 total: 30 units / 120.00

SALES_DAILY_ROWS = [
    {"sales_date": "2026-05-04", "tcin": 100, "location_id": 2750,
     "sale_quantity": 10, "sale_amount": 30.0},
    {"sales_date": "2026-05-04", "tcin": 100, "location_id": 3275,
     "sale_quantity": 7, "sale_amount": 21.0},
    {"sales_date": "2026-05-04", "tcin": 200, "location_id": 2750,
     "sale_quantity": 3, "sale_amount": 12.0},
    {"sales_date": "2026-05-05", "tcin": 100, "location_id": 2750,
     "sale_quantity": 5, "sale_amount": 15.0},
]

INVENTORY_DAILY_ROWS = [
    {"business_d": "2026-04-21", "tcin": 100, "location_id": 2750,
     "ending_on_hand_q": 50},
    {"business_d": "2026-04-22", "tcin": 100, "location_id": 2750,
     "ending_on_hand_q": 45},
    {"business_d": "2026-04-23", "tcin": 100, "location_id": 2750,
     "ending_on_hand_q": 40},
    {"business_d": "2026-04-22", "tcin": 100, "location_id": 3275,
     "ending_on_hand_q": 99},
]


# ---------------------------------------------------------------------------
# bpd_run_sql — safety gate (no query issued)
# ---------------------------------------------------------------------------


async def test_run_sql_refuses_a_writable_connection() -> None:
    """`read_only` is checked first, before the SQL is even parsed."""
    resp = await run_sql(
        _WritableWarehouse(),  # type: ignore[arg-type]
        RunSqlInput(sql="SELECT 1", response_format="json"),
    )
    assert resp.ok is False
    assert resp.error.code == "SQL_BLOCKED"
    assert "read-only" in resp.error.message


@pytest.mark.parametrize(
    "blocked",
    [
        "DROP TABLE sales_daily",
        "INSERT INTO sales_daily VALUES (1)",
        "DELETE FROM sales_daily",
        "CREATE TABLE evil AS SELECT 1",
        "UPDATE sales_daily SET tcin = 1",
        "MERGE INTO sales_daily USING x ON 1=1",
        # Two statements: the second one is the payload.
        "SELECT 1; DROP TABLE sales_daily",
        # Cloaked BEHIND a comment — layer 3 strips comments before the token
        # scan, so the DROP is still seen. (A DROP *inside* the comment is inert
        # and is deliberately allowed through; that is not this case.)
        "/* harmless preamble */ DROP TABLE sales_daily",
        "-- just a note\nTRUNCATE TABLE sales_daily",
        # BigQuery scripting / DDL-adjacent verbs.
        "EXPORT DATA OPTIONS(uri='gs://x') AS SELECT 1",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "DECLARE x INT64",
    ],
)
async def test_run_sql_blocks_non_select(blocked: str) -> None:
    """Blocked SQL never reaches the dry-run gate, so it costs nothing.

    `_NeverQueried` is what makes "costs nothing" testable: if validation ever
    regressed to blocking at execution time, the dry-run gate would touch the
    client, its AssertionError would be caught by the gate's `except Exception`,
    and the code would come back SQL_PLAN_FAILED instead of SQL_BLOCKED.
    """
    resp = await run_sql(
        _offline_warehouse(), RunSqlInput(sql=blocked, response_format="json")
    )
    assert resp.ok is False, f"failed to block: {blocked!r}"
    assert resp.error.code == "SQL_BLOCKED"


# ---------------------------------------------------------------------------
# bpd_run_sql — real execution over injected fixture CTEs
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_run_sql_resolves_bare_logical_names_through_cte_injection(
    fixture_warehouse: Any,
) -> None:
    """A bare `FROM sales_daily` works because execute_sql injects the CTE.

    `sales_daily` exists in no BigQuery catalogue — it is a registry entry — so
    this both proves injection happens and pins the aggregate.
    """
    wh = fixture_warehouse(sales_daily=SALES_DAILY_ROWS)
    resp = await run_sql(
        wh,
        RunSqlInput(
            sql=(
                "SELECT tcin, SUM(sale_quantity) AS units "
                "FROM sales_daily GROUP BY tcin ORDER BY tcin"
            ),
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["rows"] == [
        {"tcin": 100, "units": 22.0},  # 10 + 7 + 5
        {"tcin": 200, "units": 3.0},
    ]
    assert resp.data["columns"] == ["tcin", "units"]
    assert resp.data["row_count"] == 2
    # The whole point of the fixture-CTE tier: no table is scanned.
    assert resp.data["estimated_bytes_scanned"] == 0


@pytest.mark.bq
async def test_run_sql_limit_is_applied_outside_the_user_query(
    fixture_warehouse: Any,
) -> None:
    """`wrap_with_limit` caps rows without clobbering the caller's ORDER BY."""
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    resp = await run_sql(
        wh,
        RunSqlInput(
            sql="SELECT sale_quantity FROM sales_weekly ORDER BY sale_quantity DESC",
            limit=2,
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["limit"] == 2
    # Five fixture rows, capped at two, and the caller's DESC ordering survives
    # the wrapper (50 and 40 are the two largest).
    assert [r["sale_quantity"] for r in resp.data["rows"]] == [50.0, 40.0]


@pytest.mark.bq
async def test_run_sql_unknown_name_fails_in_the_dry_run_gate(
    fixture_warehouse: Any,
) -> None:
    """The dry run replaces the DuckDB EXPLAIN gate, so a bad name is caught
    before execution and reported as SQL_PLAN_FAILED, not SQL_EXECUTION_FAILED."""
    wh = fixture_warehouse(sales_daily=SALES_DAILY_ROWS)
    resp = await run_sql(
        wh,
        RunSqlInput(sql="SELECT * FROM no_such_table", response_format="json"),
    )
    assert resp.ok is False
    assert resp.error.code == "SQL_PLAN_FAILED"
    assert "no_such_table" in resp.error.message


@pytest.mark.bq
async def test_run_sql_gate_rejects_a_query_over_the_byte_ceiling(
    bq_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost ceiling, exercised against a REAL production table.

    Still 0 bytes billed: the gate rejects on the dry-run estimate and the
    query never executes. That is the only way to test the guard honestly —
    a fixture CTE estimates 0 bytes and can never trip a positive limit.
    """
    from bpd_mcp.config import Settings

    real = BigQueryWarehouse(client=bq_client)
    tiny = Settings(bpd_bq_max_bytes_billed=1024, bpd_bq_warn_bytes=1024)
    monkeypatch.setattr("bpd_mcp.tools.query.get_settings", lambda: tiny)

    resp = await run_sql(
        real,
        RunSqlInput(
            sql="SELECT tcin, sale_quantity FROM sales_daily",
            response_format="json",
        ),
    )
    assert resp.ok is False
    assert resp.error.code == "QUERY_TOO_EXPENSIVE"
    assert resp.error.details["max_bytes_billed"] == 1024
    assert resp.error.details["estimated_bytes"] > 1024


# ---------------------------------------------------------------------------
# bpd_get_sales_summary
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_sales_summary_week_grain_buckets_to_monday(
    fixture_warehouse: Any,
) -> None:
    """Week buckets are DATE_TRUNC(x, WEEK(MONDAY)) — deliberate DuckDB parity.

    THIS LOOKS WRONG AGAINST TARGET'S CALENDAR, AND THAT IS THE POINT. Target's
    fiscal week runs Sunday → Saturday, so the Saturday 2026-05-02 week-end row
    belongs to the fiscal week beginning Sunday 2026-04-26. `WEEK(MONDAY)`
    instead buckets it back to Monday 2026-04-27 — the preceding Monday. DuckDB's
    `date_trunc('week', x)` was Monday-anchored and the BigQuery swap was
    required to move no reported number, so the mismatch is preserved verbatim.
    Changing it to WEEK(SUNDAY) is a separate, deliberate decision; if someone
    makes it, this test SHOULD fail and be updated with them.
    """
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="week", response_format="json")
    )
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    by_bucket = {r["bucket"]: r for r in rows}
    # Monday parity, not the Sunday 04-26 / 05-03 fiscal week-begins.
    assert sorted(by_bucket) == [date(2026, 4, 27), date(2026, 5, 4)]
    assert by_bucket[date(2026, 4, 27)]["total_units"] == 100.0  # 50 + 30 + 20
    assert by_bucket[date(2026, 4, 27)]["total_dollars"] == pytest.approx(320.0)
    assert by_bucket[date(2026, 5, 4)]["total_units"] == 50.0  # 40 + 10
    assert by_bucket[date(2026, 5, 4)]["total_dollars"] == pytest.approx(160.0)
    assert resp.data["source_grain"] == "week"
    # Only sales_weekly is registered here, so there is no other grain to offer.
    assert resp.data["alternative_source"] is None


@pytest.mark.bq
async def test_sales_summary_day_grain_and_filters(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(sales_daily=SALES_DAILY_ROWS)
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="day", response_format="json")
    )
    assert resp.ok is True, resp.error
    by_bucket = {r["bucket"]: r for r in resp.data["rows"]}
    assert by_bucket[date(2026, 5, 4)]["total_units"] == 20.0  # 10 + 7 + 3
    assert by_bucket[date(2026, 5, 4)]["total_dollars"] == pytest.approx(63.0)
    assert by_bucket[date(2026, 5, 5)]["total_units"] == 5.0

    # tcin filter: only TCIN 100's 10 + 7 + 5.
    only_100 = await get_sales_summary(
        wh, SalesSummaryInput(grain="day", tcin=100, response_format="json")
    )
    assert sum(r["total_units"] for r in only_100.data["rows"]) == 22.0

    # location filter routes through the resolved location column.
    only_3275 = await get_sales_summary(
        wh, SalesSummaryInput(grain="day", location_id=3275, response_format="json")
    )
    assert [r["total_units"] for r in only_3275.data["rows"]] == [7.0]

    # date window is inclusive on both ends.
    one_day = await get_sales_summary(
        wh,
        SalesSummaryInput(
            grain="day",
            start_date=date(2026, 5, 5),
            end_date=date(2026, 5, 5),
            response_format="json",
        ),
    )
    assert [r["bucket"] for r in one_day.data["rows"]] == [date(2026, 5, 5)]
    assert one_day.data["effective_start"] == "2026-05-05"
    assert one_day.data["effective_end"] == "2026-05-05"


@pytest.mark.bq
async def test_sales_summary_month_grain_sums_across_weeks(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="month", response_format="json")
    )
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    assert len(rows) == 1
    assert rows[0]["bucket"] == date(2026, 5, 1)
    assert rows[0]["total_units"] == 150.0  # all five rows
    # Month from weekly rows: the straddle caveat must be stated, not implied.
    assert "week_straddle_note" in resp.data


@pytest.mark.bq
async def test_sales_summary_format_toggle(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    md = await get_sales_summary(
        wh, SalesSummaryInput(grain="week", response_format="markdown")
    )
    js = await get_sales_summary(
        wh, SalesSummaryInput(grain="week", response_format="json")
    )
    assert md.format == "markdown"
    assert md.rendered.startswith("### Sales summary (week")
    assert "| bucket | total_units | total_dollars" in md.rendered
    # The markdown branch renders the same numbers as the JSON branch.
    assert "| 2026-04-27 | 100.0 | 320.0" in md.rendered

    assert js.format == "json"
    parsed = json.loads(js.rendered)
    assert parsed["row_count"] == 2
    assert parsed["rows"][0]["bucket"] == "2026-04-27"
    # Both formats carry the same `data` payload; only `rendered` differs.
    assert md.data["rows"] == js.data["rows"]


@pytest.mark.bq
async def test_sales_summary_location_filter_needs_a_location_column(
    fixture_warehouse: Any,
) -> None:
    """A chain-level sales table + location_id is a hard error, not a silent
    unfiltered total."""
    wh = fixture_warehouse(
        sales_weekly=[
            {"sales_date": "2026-05-02", "tcin": 100, "sale_quantity": 50},
            {"sales_date": "2026-05-09", "tcin": 100, "sale_quantity": 40},
        ]
    )
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="week", location_id=2750, response_format="json")
    )
    assert resp.ok is False
    assert resp.error.code == "SCHEMA_INCOMPATIBLE"
    assert "location" in resp.error.message


async def test_sales_summary_without_a_sales_table_is_data_unavailable() -> None:
    """No sales table in the registry — decided from the registry alone, so no
    query is issued."""
    resp = await get_sales_summary(
        _offline_warehouse(), SalesSummaryInput(response_format="json")
    )
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert resp.error.details["dataset"] == "sales_daily/sales_weekly"


# ---------------------------------------------------------------------------
# bpd_get_top_skus
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_top_skus_ranks_by_units_then_by_dollars(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    units = await get_top_skus(
        wh, TopSkusInput(by="units", top_n=10, response_format="json")
    )
    assert units.ok is True, units.error
    assert [(r["tcin"], r["metric_total"]) for r in units.data["rows"]] == [
        (100, 120.0),  # 50 + 30 + 40
        (200, 30.0),   # 20 + 10
    ]
    assert units.data["metric_col"] == "sale_quantity"

    dollars = await get_top_skus(
        wh, TopSkusInput(by="dollars", top_n=10, response_format="json")
    )
    assert [(r["tcin"], r["metric_total"]) for r in dollars.data["rows"]] == [
        (100, pytest.approx(360.0)),  # 150 + 90 + 120
        (200, pytest.approx(120.0)),  # 80 + 40
    ]
    assert dollars.data["metric_col"] == "sale_amount"

    # A no-arg call must say what period it actually spans.
    assert units.data["effective_start"] == "2026-05-02"
    assert units.data["effective_end"] == "2026-05-09"


@pytest.mark.bq
async def test_top_skus_top_n_truncates_the_ranking(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY_ROWS)
    resp = await get_top_skus(
        wh, TopSkusInput(by="units", top_n=1, response_format="json")
    )
    assert [r["tcin"] for r in resp.data["rows"]] == [100]


@pytest.mark.bq
async def test_top_skus_falls_back_to_units_when_there_is_no_dollar_column(
    fixture_warehouse: Any,
) -> None:
    """`by='dollars'` on a units-only table degrades to units rather than
    failing — and says which column it actually used."""
    wh = fixture_warehouse(
        sales_weekly=[
            {"sales_date": "2026-05-02", "tcin": 100, "location_id": 2750,
             "sale_quantity": 50},
            {"sales_date": "2026-05-02", "tcin": 200, "location_id": 2750,
             "sale_quantity": 20},
        ]
    )
    resp = await get_top_skus(
        wh, TopSkusInput(by="dollars", top_n=10, response_format="json")
    )
    assert resp.ok is True, resp.error
    assert resp.data["metric_col"] == "sale_quantity"
    assert resp.data["metric_role"] == "dollars"
    assert [(r["tcin"], r["metric_total"]) for r in resp.data["rows"]] == [
        (100, 50.0),
        (200, 20.0),
    ]


@pytest.mark.bq
async def test_top_skus_points_at_the_other_grains_coverage(
    fixture_warehouse: Any,
) -> None:
    """With both grains registered, top_skus reads sales_weekly but must
    disclose that sales_daily reaches further back."""
    wh = fixture_warehouse(
        sales_weekly=SALES_WEEKLY_ROWS, sales_daily=SALES_DAILY_ROWS
    )
    resp = await get_top_skus(
        wh, TopSkusInput(by="units", top_n=10, response_format="json")
    )
    assert resp.data["table"] == "sales_weekly"
    alt = resp.data["alternative_source"]
    assert alt["table"] == "sales_daily"
    assert alt["min_date"] == "2026-05-04"
    assert alt["max_date"] == "2026-05-05"
    assert alt["scope"] == "entire table, unfiltered"


# ---------------------------------------------------------------------------
# bpd_get_inventory_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_inventory_snapshot_picks_the_latest_row_per_pair(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(inventory_daily=INVENTORY_DAILY_ROWS)
    resp = await get_inventory_snapshot(
        wh, InventorySnapshotInput(as_of=date(2026, 4, 23), response_format="json")
    )
    assert resp.ok is True, resp.error
    by_pair = {(r["tcin"], r["location_id"]): r for r in resp.data["rows"]}
    assert by_pair[(100, 2750)]["on_hand"] == 40  # 04-23 beats 04-22 and 04-21
    assert by_pair[(100, 2750)]["as_of_date"] == date(2026, 4, 23)
    assert by_pair[(100, 3275)]["on_hand"] == 99  # only one row, 04-22
    assert resp.data["on_hand_col"] == "ending_on_hand_q"


@pytest.mark.bq
async def test_inventory_snapshot_as_of_clips_the_history(
    fixture_warehouse: Any,
) -> None:
    """as_of is a real cutoff: the pair's 04-23 row must become invisible."""
    wh = fixture_warehouse(inventory_daily=INVENTORY_DAILY_ROWS)
    resp = await get_inventory_snapshot(
        wh, InventorySnapshotInput(as_of=date(2026, 4, 22), response_format="json")
    )
    by_pair = {(r["tcin"], r["location_id"]): r["on_hand"] for r in resp.data["rows"]}
    assert by_pair == {(100, 2750): 45, (100, 3275): 99}
    assert resp.data["staleness"]["window_max_date"] == "2026-04-22"


@pytest.mark.bq
async def test_inventory_snapshot_tcin_and_location_filters(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(inventory_daily=INVENTORY_DAILY_ROWS)
    resp = await get_inventory_snapshot(
        wh,
        InventorySnapshotInput(
            as_of=date(2026, 4, 23), location_id=3275, response_format="json"
        ),
    )
    assert [(r["location_id"], r["on_hand"]) for r in resp.data["rows"]] == [(3275, 99)]


# ---------------------------------------------------------------------------
# bpd_describe_schema
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_describe_schema_reports_the_real_registry(bq_client: Any) -> None:
    """describe() against the REAL registry — 15 dry runs plus one `__TABLES__`
    query, all 0 bytes. Deliberately not a fixture registry: the thing worth
    pinning is that every registered CTE body still compiles and that the
    row-count basis is labelled honestly.
    """
    wh = BigQueryWarehouse(client=bq_client)
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="json"))
    assert resp.ok is True
    tables = resp.data["tables"]
    for name in (
        "sales_daily",
        "sales_weekly",
        "inventory_daily",
        "orders_daily",
        "po_plan_daily",
        "po_plan_biweekly",
        "forecast_weekly",
        "item_attr",
        "location_attr",
        "gross_margin",
    ):
        assert name in tables, f"{name} missing from describe()"
        assert tables[name]["columns"], f"{name} reported no columns"

    # Real Target columns, not idealized ones.
    sales_cols = {c["name"] for c in tables["sales_daily"]["columns"]}
    assert {"sales_date", "tcin", "location_id", "sale_quantity", "sale_amount"} <= sales_cols

    # row_count is the BASE table's count, which overstates every deduped
    # logical table — the basis label and the note are what keep that honest.
    orders = tables["orders_daily"]
    assert orders["row_count_basis"] == "base_table"
    assert orders["source"].endswith("bpd_raw.daily_order_tcin_loc")
    assert "latest snapshot per" in orders["latest_state_note"]


@pytest.mark.bq
async def test_describe_schema_markdown_surfaces_the_row_count_basis(
    bq_client: Any,
) -> None:
    """A reader of the rendered table must not mistake ~147k base rows for the
    ~7.7k rows orders_daily actually yields."""
    wh = BigQueryWarehouse(client=bq_client)
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="markdown"))
    assert resp.format == "markdown"
    assert "#### `sales_daily`" in resp.rendered
    assert "base rows in biom-reporting-s26." in resp.rendered
    # The dedup note is rendered as a blockquote right under the heading.
    assert "> Reduced to the latest snapshot per" in resp.rendered
