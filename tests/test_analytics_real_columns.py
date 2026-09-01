"""Analytics-tool tests against Target's *real* column names — the migration's
main safety net.

WHAT THIS FILE PINS. Every analytics tool resolves its columns at call time
through `column_roles.resolve_column`, against the live schema of a logical
table's CTE body. This file asserts that the roles land on the names Target
actually ships (`sale_quantity`, `sales_date`, `business_d`, `ending_on_hand_q`,
`selected_forecast_q`, `fiscal_week_begin_d`, `last_update_d`, `revised_order_q`,
`item_received_q`, `receiving_location_id`, `ordered_q`, `order_d`) AND that the
number each tool then computes over known rows is the number worked out by hand
in the fixture's own comment. A test that only asserted "no exception" would
have caught none of the bugs this migration actually had.

TIER. `@pytest.mark.bq`: real BigQuery SQL over literal fixture CTEs, 0 bytes
billed, ~0.7 s a query (see conftest.py for why there is no local engine
double — a DuckDB stand-in goes green exactly when production is broken). The
few checks that provably return before any query is planned stay in the default
tier.

TWO DIFFERENT WEEK CONCEPTS LIVE HERE. Do not harmonize them:

  * `DATE_TRUNC(x, WEEK(MONDAY))` in get_sales_summary / get_upcoming_pos is
    bug-for-bug parity with DuckDB's Monday-anchored `date_trunc('week', x)`.
    It disagrees with Target's Sunday→Saturday fiscal week ON PURPOSE, because
    a data-layer swap must not move a reported number. The tests below assert
    the Monday behaviour and say so where it looks wrong.
  * The `+6` / `-6` day shift in get_forecast_vs_actual is Target's real fiscal
    week-END anchor: `fiscal_week_begin_d` is a Sunday in 100% of forecast rows
    and every weekly sales date is a Saturday, so begin + 6 = the week-end the
    two sides join on. That one is correct as written.

DATE ANCHORS. Fixtures that the tool compares against `today` (forecast
windows, upcoming-PO horizons) are built relative to the most recent Saturday
so the suite does not rot; `weeks_back` is capped at 104 by the schema, and
absolute 2026 dates would silently fall out of that window in 2028.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import conftest as _conftest
import pytest
from conftest import fixture_table

from bpd_mcp.bq import LOGICAL_TABLES, BigQueryWarehouse, LogicalTable
from bpd_mcp.schemas import (
    ForecastVsActualInput,
    InventorySnapshotInput,
    OpenOrdersInput,
    SalesSummaryInput,
    SellThroughInput,
    TopSkusInput,
    UpcomingPosInput,
)
from bpd_mcp.tools.query import (
    get_forecast_vs_actual,
    get_inventory_snapshot,
    get_open_orders,
    get_sales_summary,
    get_sell_through,
    get_top_skus,
    get_upcoming_pos,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def typed_table(
    name: str,
    rows: list[dict[str, Any]],
    *,
    date_column: str | None = None,
    columns: list[str] | None = None,
    types: dict[str, str] | None = None,
) -> LogicalTable:
    """`conftest.fixture_table`, with a per-call override of a column's type.

    Two things need it, and neither is "the shared map is incomplete" — the
    production DATE columns this module uses (`last_update_d`,
    `fiscal_week_end_d`) are pinned in `conftest._TYPES`:

      * A column production does NOT have. `week_end_date` appears in
        `COLUMN_ROLES` as a candidate for a shape Target might ship but does
        not today, so it has no production type to pin globally; a fixture
        that exercises the week-END anchor states DATE here instead.
      * A DELIBERATE downgrade. Several bpd_raw feeds ship a logically-DATE
        column as STRING holding Target's literal `""` placeholder. Pinning
        STRING for one fixture is how that data state is expressed, and saying
        it out loud beats relying on the column being absent from `_TYPES`.

    The map is restored afterwards so nothing leaks into another test module.
    """
    if not types:
        return fixture_table(name, rows, date_column=date_column, columns=columns)
    saved = {k: _conftest._TYPES.get(k) for k in types}
    _conftest._TYPES.update(types)
    try:
        return fixture_table(name, rows, date_column=date_column, columns=columns)
    finally:
        for k, v in saved.items():
            if v is None:
                _conftest._TYPES.pop(k, None)
            else:
                _conftest._TYPES[k] = v


def empty_like(table: LogicalTable) -> LogicalTable:
    """The same table, same column types, zero rows.

    `fixture_table` needs at least one row to infer types, so "present but
    empty" — a real and distinct data state — is expressed by filtering the
    typed body away.
    """
    return LogicalTable(
        name=table.name,
        sql=f"SELECT * FROM (\n{table.sql}\n) WHERE FALSE",
        base_tables=(),
        date_column=table.date_column,
        patterns=(),
    )


class _NeverQueried:
    """Stands in for a `bigquery.Client` the code under test must not touch."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - only on failure
        raise AssertionError(f"BigQuery client was touched (.{name}) in an offline test")


# ---- date anchors -----------------------------------------------------------

TODAY = date.today()
#: The most recent Saturday on or before today — Target's fiscal week-END anchor.
LAST_SATURDAY = TODAY - timedelta(days=(TODAY.weekday() - 5) % 7)


def week_end(n: int) -> date:
    """Saturday ending the fiscal week `n` weeks before the latest one."""
    return LAST_SATURDAY - timedelta(weeks=n)


def week_begin(n: int) -> date:
    """Sunday opening the same fiscal week (= week_end(n) - 6)."""
    return week_end(n) - timedelta(days=6)


def iso(d: date) -> str:
    return d.isoformat()


# ---- orders_daily: the REAL registry body over literal rows ------------------

_ORDERS = LOGICAL_TABLES["orders_daily"]
#: (SOURCE_COLUMN, projected_alias) pairs read out of the real registry body.
_ORDERS_PAIRS = [
    (src, alias)
    for src, alias in re.findall(
        r"([A-Z][A-Z0-9_]*)\s+AS\s+([a-z][a-z0-9_]*)", _ORDERS.sql
    )
    if alias == src.lower()
]
_STANDARD_TYPE = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def _literal(sql_type: str, value: Any) -> str:
    t = _STANDARD_TYPE.get(sql_type, sql_type)
    if value is None:
        return f"CAST(NULL AS {t})"
    if t == "DATE":
        return f"DATE '{value}'"
    if t == "STRING":
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    if t == "BOOL":
        return "TRUE" if value else "FALSE"
    return f"CAST({value} AS {t})"


def orders_latest_state_table(
    rows: list[dict[str, Any]], *, types: dict[str, str], marker: str = ""
) -> LogicalTable:
    """orders_daily's PRODUCTION body with its base table swapped for literal rows.

    Not a re-implementation: `LOGICAL_TABLES["orders_daily"].sql` is used
    verbatim, with only the `FROM \\`...daily_order_tcin_loc\\`` reference
    replaced. That means the QUALIFY latest-state reduction under test is the
    exact text production runs, over rows whose right answer is known.

    `marker` is emitted as a comment inside the swapped subquery. BigQuery's
    results cache is keyed on the exact query text, so varying it is what makes
    "identical across consecutive UNCACHED runs" a real claim rather than a
    cache hit.
    """
    selects = []
    for i, row in enumerate(rows):
        cells = [
            _literal(types[alias], row.get(alias)) + (f" AS {src}" if i == 0 else "")
            for src, alias in _ORDERS_PAIRS
        ]
        selects.append("SELECT " + ", ".join(cells))
    inner = "\n UNION ALL ".join(selects)
    body = _ORDERS.sql.replace(
        f"`{_ORDERS.primary_base_table}`", f"(\n-- {marker}\n{inner}\n)"
    )
    assert body != _ORDERS.sql, "base-table reference not found — registry body changed"
    return LogicalTable(
        name="orders_daily",
        sql=body,
        base_tables=(),
        date_column=_ORDERS.date_column,
        patterns=(),
        latest_state_note=_ORDERS.latest_state_note,
    )


@pytest.fixture(scope="module")
def orders_base_types(bq_client: Any) -> dict[str, str]:
    """Production column types for orders_daily, from a 0-byte dry run.

    Derived rather than hardcoded so the fixture cannot drift into typing a
    column differently from the table it stands in for.
    """
    return dict(BigQueryWarehouse(client=bq_client).logical_schema("orders_daily"))


# ---------------------------------------------------------------------------
# Shared fixture rows, with the arithmetic worked out in comments
# ---------------------------------------------------------------------------

SALES_DAILY = [
    {"sales_date": "2026-05-04", "tcin": 100, "location_id": 2750,
     "sale_quantity": 10, "sale_amount": 30.0},
    {"sales_date": "2026-05-04", "tcin": 100, "location_id": 3275,
     "sale_quantity": 7, "sale_amount": 21.0},
    {"sales_date": "2026-05-04", "tcin": 200, "location_id": 2750,
     "sale_quantity": 3, "sale_amount": 12.0},
    {"sales_date": "2026-05-05", "tcin": 100, "location_id": 2750,
     "sale_quantity": 5, "sale_amount": 15.0},
]
# 05-04: 10 + 7 + 3 = 20 units / 63.00 ; 05-05: 5 units / 15.00

SALES_WEEKLY = [
    {"sales_date": "2026-05-09", "tcin": 100, "location_id": 2750,
     "sale_quantity": 50, "sale_amount": 150.0},
    {"sales_date": "2026-05-09", "tcin": 100, "location_id": 3275,
     "sale_quantity": 30, "sale_amount": 90.0},
    {"sales_date": "2026-05-09", "tcin": 200, "location_id": 2750,
     "sale_quantity": 12, "sale_amount": 48.0},
]
# tcin 100 = 80 units / 240.00 ; tcin 200 = 12 units / 48.00

INVENTORY_DAILY = [
    {"business_d": "2026-05-04", "tcin": 100, "location_id": 2750,
     "beginning_on_hand_q": 210, "ending_on_hand_q": 200},
    {"business_d": "2026-05-05", "tcin": 100, "location_id": 2750,
     "beginning_on_hand_q": 200, "ending_on_hand_q": 195},
    {"business_d": "2026-05-05", "tcin": 100, "location_id": 3275,
     "beginning_on_hand_q": 160, "ending_on_hand_q": 150},
    {"business_d": "2026-05-05", "tcin": 200, "location_id": 2750,
     "beginning_on_hand_q": 80, "ending_on_hand_q": 75},
]


# ===========================================================================
# 1. Each tool resolves the REAL Target column names and computes the right
#    number over them.
# ===========================================================================


@pytest.mark.bq
async def test_sales_summary_resolves_real_sales_columns(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(sales_daily=SALES_DAILY)
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="day", response_format="json")
    )
    assert resp.ok is True, resp.error
    assert resp.data["date_col"] == "sales_date"
    assert resp.data["date_col_type"] == "DATE"
    assert resp.data["units_col"] == "sale_quantity"
    assert resp.data["dollars_col"] == "sale_amount"
    by_bucket = {r["bucket"]: r for r in resp.data["rows"]}
    assert by_bucket[date(2026, 5, 4)]["total_units"] == 20.0
    assert by_bucket[date(2026, 5, 4)]["total_dollars"] == pytest.approx(63.0)
    assert by_bucket[date(2026, 5, 5)]["total_units"] == 5.0


@pytest.mark.bq
async def test_sales_summary_survives_targets_empty_string_date_placeholder(
    fixture_warehouse: Any,
) -> None:
    """`select_as_date()` must emit SAFE_CAST, not CAST.

    Several bpd_raw feeds ship dates as STRING and pad absent values with
    Target's literal `""` rather than NULL. A plain `CAST('""' AS DATE)` is a
    hard 400 that takes down the WHOLE query — optimizer-dependently, so a
    smoke query over clean rows proves nothing. Under SAFE_CAST the bad row
    lands in a NULL bucket and every other bucket is unaffected.
    """
    wh = fixture_warehouse(
        sales_weekly=typed_table(
            "sales_weekly",
            [
                {"fiscal_week_end_d": "2026-05-02", "tcin": 100,
                 "location_id": 2750, "sale_quantity": 50},
                {"fiscal_week_end_d": "2026-05-09", "tcin": 100,
                 "location_id": 2750, "sale_quantity": 40},
                {"fiscal_week_end_d": '""', "tcin": 100,
                 "location_id": 2750, "sale_quantity": 7},
            ],
            date_column="fiscal_week_end_d",
            # fiscal_week_end_d is DATE in production and conftest pins it so.
            # This fixture stands in for the bpd_raw feeds that ship it as
            # STRING, so the downgrade is stated, not inherited from a gap in
            # the type map — the assertion on date_col_type below depends on it.
            types={"fiscal_week_end_d": "STRING"},
        )
    )
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="day", response_format="json")
    )
    assert resp.ok is True, resp.error
    assert resp.data["date_col"] == "fiscal_week_end_d"
    assert resp.data["date_col_type"] == "STRING", (
        "the point of this fixture is the STRING date column that forces the "
        "SAFE_CAST branch"
    )
    by_bucket = {r["bucket"]: r["total_units"] for r in resp.data["rows"]}
    assert by_bucket[date(2026, 5, 2)] == 50.0
    assert by_bucket[date(2026, 5, 9)] == 40.0
    # The placeholder row is NOT dropped and NOT fatal — it is uncastable, so
    # it aggregates into a NULL bucket where a reader can see it.
    assert by_bucket[None] == 7.0


@pytest.mark.bq
async def test_top_skus_resolves_real_sales_columns(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_WEEKLY, sales_daily=SALES_DAILY)
    resp = await get_top_skus(
        wh, TopSkusInput(by="units", top_n=10, response_format="json")
    )
    assert resp.ok is True, resp.error
    assert resp.data["metric_col"] == "sale_quantity"
    assert [(r["tcin"], r["metric_total"]) for r in resp.data["rows"]] == [
        (100, 80.0),  # 50 + 30
        (200, 12.0),
    ]
    # A no-arg call must state the period it spans and point at the other grain.
    assert resp.data["effective_start"] == "2026-05-09"
    assert resp.data["effective_end"] == "2026-05-09"
    assert resp.data["alternative_source"]["table"] == "sales_daily"
    assert resp.data["alternative_source"]["min_date"] == "2026-05-04"
    assert resp.data["alternative_source"]["scope"] == "entire table, unfiltered"


@pytest.mark.bq
async def test_inventory_snapshot_resolves_ending_on_hand_q(
    fixture_warehouse: Any,
) -> None:
    """P0-1 regression guard.

    Real inventory_daily ships `beginning_on_hand_q` AND `ending_on_hand_q`.
    The period-END value is what "on hand" means, and the candidate list orders
    it first; this exact shape hard-failed before Patch #10.
    """
    wh = fixture_warehouse(inventory_daily=INVENTORY_DAILY)
    resp = await get_inventory_snapshot(
        wh, InventorySnapshotInput(as_of=date(2026, 5, 5), response_format="json")
    )
    assert resp.ok is True, resp.error
    assert resp.data["date_col"] == "business_d"
    assert resp.data["on_hand_col"] == "ending_on_hand_q"
    by_pair = {(r["tcin"], r["location_id"]): r["on_hand"] for r in resp.data["rows"]}
    # Latest day per pair, and the ENDING value — never the 210/200/160/80
    # beginning_ bookends.
    assert by_pair == {(100, 2750): 195, (100, 3275): 150, (200, 2750): 75}


@pytest.mark.bq
async def test_sell_through_resolves_both_tables_and_computes_the_ratio(
    fixture_warehouse: Any,
) -> None:
    """Sell-through joins two feeds whose column vocabularies differ
    (`sale_quantity`/`sales_date` vs `ending_on_hand_q`/`business_d`)."""
    wh = fixture_warehouse(
        sales_weekly=[
            # tcin 1 @ loc 5: 40 + 20 = 60 units over 2 distinct weeks.
            {"sales_date": "2026-07-18", "tcin": 1, "location_id": 5,
             "sale_quantity": 40},
            {"sales_date": "2026-07-25", "tcin": 1, "location_id": 5,
             "sale_quantity": 20},
            # tcin 2 @ loc 5: 10 units, and NO inventory row at all.
            {"sales_date": "2026-07-25", "tcin": 2, "location_id": 5,
             "sale_quantity": 10},
        ],
        inventory_daily=[
            {"business_d": "2026-07-24", "tcin": 1, "location_id": 5,
             "ending_on_hand_q": 44},
            {"business_d": "2026-07-30", "tcin": 1, "location_id": 5,
             "ending_on_hand_q": 30},
        ],
    )
    resp = await get_sell_through(wh, SellThroughInput(response_format="json"))
    assert resp.ok is True, resp.error
    cols = resp.data["resolved_columns"]
    assert cols["sales_units"] == "sale_quantity"
    assert cols["sales_date"] == "sales_date"
    assert cols["inv_on_hand"] == "ending_on_hand_q"
    assert cols["inv_date"] == "business_d"
    assert cols["inv_location"] == "location_id"

    rows = {r["tcin"]: r for r in resp.data["rows"]}
    # tcin 1: latest on-hand is the 07-30 row (30), not the 07-24 one (44).
    assert rows[1]["units_sold"] == 60.0
    assert rows[1]["on_hand"] == 30
    # weeks_of_supply = on_hand / (units_sold / weeks_observed) = 30 / (60/2) = 1.0
    assert rows[1]["weeks_of_supply"] == pytest.approx(1.0)
    # sell_through_rate = 60 / (60 + 30) = 0.6667
    assert rows[1]["sell_through_rate"] == pytest.approx(60 / 90)
    # tcin 2 has no inventory row: LEFT JOIN keeps it with on_hand NULL, and
    # the rate is 10/(10+0) = 1.0 — correct, since there is nothing left.
    assert rows[2]["on_hand"] is None
    assert rows[2]["sell_through_rate"] == pytest.approx(1.0)


@pytest.mark.bq
async def test_open_orders_derives_open_units_from_real_order_columns(
    fixture_warehouse: Any,
) -> None:
    """There is no physical "open units" column, and `purchase_order_active_f`
    is 98.1% Target's `""` placeholder — so open units are DERIVED as
    revised_order_q - item_received_q - cancel_remaining_order_q, per line."""
    wh = fixture_warehouse(
        orders_daily=[
            # PO 1 / tcin 100: 100 - 40 - 10 = 50 open.
            {"snapshot_d": "2026-07-30", "purchase_order_id": 1,
             "purchase_order_create_d": "2026-06-01", "tcin": 100,
             "receiving_location_id": 500, "original_order_q": 100,
             "revised_order_q": 100, "item_received_q": 40,
             "cancel_remaining_order_q": 10},
            # PO 1 / tcin 200: fully received → 0 open → excluded.
            {"snapshot_d": "2026-07-30", "purchase_order_id": 1,
             "purchase_order_create_d": "2026-06-01", "tcin": 200,
             "receiving_location_id": 500, "original_order_q": 30,
             "revised_order_q": 30, "item_received_q": 30,
             "cancel_remaining_order_q": 0},
            # PO 2 / tcin 100: NULL received and cancel are COALESCEd to 0 → 20.
            {"snapshot_d": "2026-07-30", "purchase_order_id": 2,
             "purchase_order_create_d": "2026-07-01", "tcin": 100,
             "receiving_location_id": 501, "original_order_q": 20,
             "revised_order_q": 20, "item_received_q": None,
             "cancel_remaining_order_q": None},
            # PO 3 / tcin 300: over-received → -5 → excluded but counted.
            {"snapshot_d": "2026-07-30", "purchase_order_id": 3,
             "purchase_order_create_d": "2026-06-15", "tcin": 300,
             "receiving_location_id": 500, "original_order_q": 15,
             "revised_order_q": 15, "item_received_q": 20,
             "cancel_remaining_order_q": 0},
        ]
    )
    resp = await get_open_orders(wh, OpenOrdersInput(response_format="json"))
    assert resp.ok is True, resp.error
    assert resp.data["resolved_columns"] == {
        "ordered": "revised_order_q",
        "received": "item_received_q",
        "cancel_remaining": "cancel_remaining_order_q",
        "po_id": "purchase_order_id",
        "location": "receiving_location_id",
        "order_created": "purchase_order_create_d",
    }
    rows = {r["tcin"]: r for r in resp.data["rows"]}
    assert rows[100]["open_units"] == 70.0  # 50 + 20
    assert rows[100]["po_count"] == 2
    assert rows[100]["line_count"] == 2
    assert 200 not in rows, "a fully-received line has nothing open"
    assert 300 not in rows, "an over-received line is negative, not open"
    # Excluded, but never silently: over-receipt is a labelled count.
    assert resp.data["over_received"] == {"lines": 1, "units_over": 5.0}

    # as_of_date is a PO-CREATION cutoff, not time travel: at 06-15 only PO 1
    # (06-01) and PO 3 (06-15) exist, and PO 3 has nothing open.
    at_0615 = await get_open_orders(
        wh, OpenOrdersInput(as_of_date=date(2026, 6, 15), response_format="json")
    )
    rows2 = {r["tcin"]: r for r in at_0615.data["rows"]}
    assert rows2[100]["open_units"] == 50.0
    assert rows2[100]["po_count"] == 1
    assert "latest-known state" in at_0615.data["scope"]

    # location_filter routes through receiving_location_id, not location_id.
    at_501 = await get_open_orders(
        wh, OpenOrdersInput(location_filter=[501], response_format="json")
    )
    assert {r["tcin"]: r["open_units"] for r in at_501.data["rows"]} == {100: 20.0}


@pytest.mark.bq
async def test_upcoming_pos_uses_latest_snapshot_and_splits_sources(
    fixture_warehouse: Any,
) -> None:
    """The two landmines: (1) without the MAX(business_d) filter the stale
    snapshot's 999 units double-count — these tables accumulate every
    generation of the plan; (2) without per-source grouping the daily 100 and
    the biweekly 500 blend into a meaningless 600."""
    fresh = iso(TODAY - timedelta(days=1))
    stale = iso(TODAY - timedelta(days=2))
    in_window = iso(TODAY + timedelta(days=3))
    out_window = iso(TODAY + timedelta(weeks=20))

    wh = fixture_warehouse(
        po_plan_daily=[
            {"business_d": stale, "tcin": 100, "order_d": in_window,
             "receiving_location_id": 500, "ordered_q": 999},
            {"business_d": fresh, "tcin": 100, "order_d": in_window,
             "receiving_location_id": 500, "ordered_q": 40},
            {"business_d": fresh, "tcin": 100, "order_d": in_window,
             "receiving_location_id": 501, "ordered_q": 60},
            {"business_d": fresh, "tcin": 100, "order_d": out_window,
             "receiving_location_id": 500, "ordered_q": 77},
        ],
        po_plan_biweekly=[
            {"business_d": fresh, "tcin": 100, "order_d": in_window,
             "receiving_location_id": 500, "ordered_q": 500},
        ],
    )
    resp = await get_upcoming_pos(
        wh, UpcomingPosInput(weeks_forward=8, response_format="json")
    )
    assert resp.ok is True, resp.error
    # 40 + 60 from the latest snapshot only. Not 999 (stale), not 77 (outside
    # the 8-week horizon), and never summed with the biweekly plan.
    assert resp.data["source_totals"] == {
        "po_plan_daily": 100,
        "po_plan_biweekly": 500,
    }
    per_row_totals: dict[str, int] = {}
    for r in resp.data["rows"]:
        per_row_totals[r["source"]] = per_row_totals.get(r["source"], 0) + r["planned_units"]
    assert per_row_totals == resp.data["source_totals"]

    resolved = resp.data["resolved_columns"]["po_plan_daily"]
    assert resolved["qty_col"] == "ordered_q"
    assert resolved["order_date_col"] == "order_d"
    assert resolved["snapshot_col"] == "business_d"
    assert resolved["latest_snapshot"] == fresh


@pytest.mark.bq
async def test_forecast_vs_actual_resolves_real_forecast_columns(
    fixture_warehouse: Any,
) -> None:
    """Coverage-honest spine: only MATCHED (tcin, location, week) cells produce
    variance. tcin 100's location-3275 actuals have no forecast, so they are
    reported as actual_only coverage rather than inflating actual_units."""
    w1b, w1e = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [
                # Two snapshots of the same forecast week.
                {"fiscal_week_begin_d": iso(w1b), "tcin": 100, "location_id": 2750,
                 "last_update_d": iso(w1b - timedelta(days=2)),
                 "selected_forecast_q": 55},
                {"fiscal_week_begin_d": iso(w1b), "tcin": 100, "location_id": 2750,
                 "last_update_d": iso(w1e + timedelta(days=3)),
                 "selected_forecast_q": 48},
                {"fiscal_week_begin_d": iso(w1b), "tcin": 200, "location_id": 2750,
                 "last_update_d": iso(w1b - timedelta(days=2)),
                 "selected_forecast_q": 10},
            ],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(w1e), "tcin": 100, "location_id": 2750,
             "sale_quantity": 50},
            {"sales_date": iso(w1e), "tcin": 100, "location_id": 3275,
             "sale_quantity": 30},
            {"sales_date": iso(w1e), "tcin": 200, "location_id": 2750,
             "sale_quantity": 12},
        ],
    )
    resp = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104,
            as_of_date=w1b - timedelta(days=1),  # before the post-week revision
            aggregate="by_sku",
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["forecast_date_col"] == "fiscal_week_begin_d"
    assert resp.data["forecast_units_col"] == "selected_forecast_q"
    assert resp.data["forecast_snapshot_col"] == "last_update_d"
    assert resp.data["actual_date_col"] == "sales_date"
    assert resp.data["actual_units_col"] == "sale_quantity"
    assert resp.data["spine"] == "tcin, location, week"

    by_tcin = {r["tcin"]: r for r in resp.data["rows"]}
    # The cutoff selects the pre-week snapshot (55), not the revision (48).
    assert by_tcin[100]["forecast_units"] == 55.0
    # Matched cell only: loc 2750's 50. Loc 3275's 30 units have no forecast.
    assert by_tcin[100]["actual_units"] == 50.0
    assert by_tcin[100]["variance_units"] == -5.0
    # variance_pct is a true percent (0-100 scale), not a ratio.
    assert by_tcin[100]["variance_pct"] == pytest.approx(100 * -5 / 55)
    assert resp.data["variance_pct_scale"] == "percent (0-100)"
    assert by_tcin[200]["forecast_units"] == 10.0
    assert by_tcin[200]["actual_units"] == 12.0

    cov = resp.data["coverage"]
    assert cov["matched"]["cells"] == 2
    assert cov["actual_only"]["cells"] == 1
    assert cov["actual_only"]["actual_units"] == 30.0


@pytest.mark.bq
async def test_forecast_snapshot_policy_picks_a_different_number(
    fixture_warehouse: Any,
) -> None:
    """latest_available takes the newest snapshot (a post-week revision);
    pre_week takes only what Target had published before the week opened."""
    w1b, w1e = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [
                {"fiscal_week_begin_d": iso(w1b), "tcin": 100, "location_id": 2750,
                 "last_update_d": iso(w1b - timedelta(days=2)),
                 "selected_forecast_q": 55},
                {"fiscal_week_begin_d": iso(w1b), "tcin": 100, "location_id": 2750,
                 "last_update_d": iso(w1e + timedelta(days=3)),
                 "selected_forecast_q": 48},
            ],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(w1e), "tcin": 100, "location_id": 2750,
             "sale_quantity": 50},
        ],
    )
    latest = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, aggregate="by_sku", response_format="json"
        ),
    )
    pre = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104,
            aggregate="by_sku",
            snapshot_policy="pre_week",
            response_format="json",
        ),
    )
    assert latest.ok and pre.ok, (latest.error, pre.error)
    assert latest.data["rows"][0]["forecast_units"] == 48.0
    assert "latest_available" in latest.data["snapshot_policy"]
    assert pre.data["rows"][0]["forecast_units"] == 55.0
    assert "pre_week" in pre.data["snapshot_policy"]
    # Any historical cutoff carries the retention caveat.
    assert "snapshot_retention_caveat" in pre.data


async def test_forecast_vs_actual_missing_table_is_decided_offline() -> None:
    """`forecast_weekly` absent from the registry: answered from the registry
    alone, so nothing is queried and nothing is billed."""
    wh = BigQueryWarehouse(client=_NeverQueried(), registry={})
    resp = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=8, response_format="json")
    )
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert resp.error.details["dataset"] == "forecast_weekly"


@pytest.mark.bq
async def test_forecast_column_miss_reports_a_diagnostic_error(
    fixture_warehouse: Any,
) -> None:
    """The error must name the dataset, the role, the candidates tried and the
    columns actually present — so "add this alias" is a one-glance fix."""
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": "2026-05-03", "tcin": 100,
              "weird_column_name_for_units": 5}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": "2026-05-09", "tcin": 100, "location_id": 1,
             "sale_quantity": 50}
        ],
    )
    resp = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=8, response_format="json")
    )
    assert resp.ok is False
    assert resp.error.code == "SCHEMA_INCOMPATIBLE"
    detail = resp.error.details
    assert detail["dataset"] == "forecast_weekly"
    assert detail["role"] == "units"
    assert "selected_forecast_q" in detail["candidates"]
    assert "weird_column_name_for_units" in detail["actual_columns"]


# ===========================================================================
# 2. Week anchoring. Two different concepts — see the module docstring.
# ===========================================================================


@pytest.mark.bq
async def test_sales_week_buckets_are_monday_anchored_not_targets_fiscal_week(
    fixture_warehouse: Any,
) -> None:
    """`DATE_TRUNC(x, WEEK(MONDAY))`, and the mismatch is deliberate.

    THIS ASSERTION LOOKS WRONG AGAINST TARGET'S CALENDAR ON PURPOSE. Target's
    fiscal week runs Sunday → Saturday: the Saturday 2026-05-02 week-end row
    belongs to the fiscal week that began Sunday 2026-04-26. WEEK(MONDAY)
    instead buckets it to Monday 2026-04-27.

    Why it stays that way: DuckDB's `date_trunc('week', x)` was Monday-anchored,
    and the BigQuery swap was required to move no reported number, so the
    oddity is reproduced bug-for-bug. Two ways to get this wrong silently —
    BigQuery's bare `WEEK` defaults to SUNDAY, and `DATE_TRUNC(x, WEEK)` flips
    the argument order versus DuckDB without being a compile error — which is
    exactly why the bucket VALUES are asserted here and not just the totals.
    Switching to WEEK(SUNDAY) is a separate decision; when someone makes it,
    this test should fail and be updated with them.
    """
    wh = fixture_warehouse(
        sales_weekly=[
            {"sales_date": "2026-05-02", "tcin": 100, "location_id": 1,
             "sale_quantity": 50},
            {"sales_date": "2026-05-09", "tcin": 100, "location_id": 1,
             "sale_quantity": 40},
        ]
    )
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="week", response_format="json")
    )
    assert resp.ok is True, resp.error
    buckets = {r["bucket"]: r["total_units"] for r in resp.data["rows"]}
    assert buckets == {date(2026, 4, 27): 50.0, date(2026, 5, 4): 40.0}
    # Neither Target's Sunday week-begin nor BigQuery's default Sunday anchor.
    assert date(2026, 4, 26) not in buckets
    assert date(2026, 5, 3) not in buckets


@pytest.mark.bq
async def test_upcoming_pos_weeks_are_monday_anchored_too(
    fixture_warehouse: Any,
) -> None:
    """The second WEEK(MONDAY) call site. Same parity decision, same reason."""
    d1 = TODAY + timedelta(days=3)
    d2 = TODAY + timedelta(days=10)
    monday1 = d1 - timedelta(days=d1.weekday())
    monday2 = d2 - timedelta(days=d2.weekday())
    wh = fixture_warehouse(
        po_plan_daily=[
            {"business_d": iso(TODAY - timedelta(days=1)), "tcin": 100,
             "order_d": iso(d1), "receiving_location_id": 500, "ordered_q": 11},
            {"business_d": iso(TODAY - timedelta(days=1)), "tcin": 100,
             "order_d": iso(d2), "receiving_location_id": 500, "ordered_q": 22},
        ]
    )
    resp = await get_upcoming_pos(
        wh, UpcomingPosInput(weeks_forward=8, response_format="json")
    )
    assert resp.ok is True, resp.error
    got = {r["week"]: r["planned_units"] for r in resp.data["rows"]}
    assert got == {monday1: 11, monday2: 22}
    assert all(w.weekday() == 0 for w in got), "buckets must land on Mondays"


@pytest.mark.bq
async def test_forecast_canonicalizes_sunday_begin_to_saturday_week_end(
    fixture_warehouse: Any,
) -> None:
    """The Patch #5 bug: zero matched rows because Sundays and Saturdays never met.

    forecast_weekly is keyed on Sunday-anchored `fiscal_week_begin_d`;
    sales_weekly is keyed on Saturday-anchored `sales_date`. The tool shifts the
    forecast side by +6 days so both describe the same fiscal week-END. This is
    Target's REAL fiscal anchor and is a different concept from the WEEK(MONDAY)
    bucketing above — harmonizing the two would shift every forecast cell by a
    week.
    """
    weeks = [(week_begin(n), week_end(n)) for n in (3, 2, 1)]
    forecast_rows = [
        {"fiscal_week_begin_d": iso(b), "tcin": 100, "location_id": 2750,
         "last_update_d": iso(b - timedelta(days=1)), "selected_forecast_q": q}
        for (b, _e), q in zip(weeks, (60, 70, 80), strict=True)
    ]
    # tcin 999: forecast with no actuals anywhere.
    forecast_rows.append(
        {"fiscal_week_begin_d": iso(weeks[2][0]), "tcin": 999,
         "location_id": 2750, "last_update_d": iso(weeks[2][0] - timedelta(days=1)),
         "selected_forecast_q": 12}
    )
    sales_rows = [
        {"sales_date": iso(e), "tcin": 100, "location_id": 2750,
         "sale_quantity": q}
        for (_b, e), q in zip(weeks, (50, 90, 75), strict=True)
    ]
    # tcin 888: actuals with no forecast.
    sales_rows.append(
        {"sales_date": iso(weeks[1][1]), "tcin": 888, "location_id": 2750,
         "sale_quantity": 5}
    )
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly", forecast_rows,
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=sales_rows,
    )

    resp = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104,
            aggregate="by_sku_week",
            as_of_date=TODAY,
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["forecast_week_anchor"] == "begin"
    assert resp.data["forecast_week_shift_days"] == 6

    by_pair = {(r["tcin"], r["week_end_date"]): r for r in resp.data["rows"]}
    # Pre-fix this dict was EMPTY: every Sunday forecast missed every Saturday
    # actual, so the FULL OUTER JOIN produced no matched cell at all.
    assert len(by_pair) == 3
    for (_b, e), fc, act in zip(weeks, (60, 70, 80), (50, 90, 75), strict=True):
        row = by_pair[(100, e)]
        assert row["forecast_units"] == float(fc)
        assert row["actual_units"] == float(act)
        assert row["variance_units"] == float(act - fc)
        assert row["variance_pct"] == pytest.approx(100 * (act - fc) / fc)

    # Unmatched cells are EXCLUDED by default — never zero-filled into a fake
    # -100% / +inf variance — and counted in coverage instead.
    assert (999, weeks[2][1]) not in by_pair
    assert (888, weeks[1][1]) not in by_pair
    cov = resp.data["coverage"]
    assert cov["matched"]["cells"] == 3
    assert cov["forecast_only"] == {
        "cells": 1, "forecast_units": 12.0, "actual_units": None
    }
    assert cov["actual_only"] == {
        "cells": 1, "forecast_units": None, "actual_units": 5.0
    }

    # The requested 104-week window is clamped to the actuals' coverage, and
    # says so rather than silently truncating.
    assert resp.data["requested_weeks_back"] == 104
    assert resp.data["effective_start"] == iso(weeks[0][1])
    assert resp.data["effective_end"] == iso(weeks[2][1])
    assert resp.data["effective_weeks_covered"] == 3
    assert resp.data["window_truncated_to_actuals_coverage"] is True

    # include_unmatched returns them with the MISSING SIDE NULL, not 0.
    with_unmatched = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104,
            aggregate="by_sku_week",
            as_of_date=TODAY,
            include_unmatched=True,
            response_format="json",
        ),
    )
    both = {(r["tcin"], r["week_end_date"]): r for r in with_unmatched.data["rows"]}
    fc_only = both[(999, weeks[2][1])]
    assert fc_only["coverage"] == "forecast_only"
    assert fc_only["forecast_units"] == 12.0
    assert fc_only["actual_units"] is None
    assert fc_only["variance_pct"] is None
    act_only = both[(888, weeks[1][1])]
    assert act_only["coverage"] == "actual_only"
    assert act_only["forecast_units"] is None
    assert act_only["actual_units"] == 5.0


@pytest.mark.bq
async def test_forecast_applies_no_shift_when_the_column_is_a_week_end(
    fixture_warehouse: Any,
) -> None:
    """If Target ever ships forecast_weekly keyed on a week-END column, the +6
    must NOT fire — it would push every cell a week forward."""
    _b, e = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"week_end_date": iso(e), "tcin": 100, "location_id": 2750,
              "last_update_d": iso(e - timedelta(days=7)),
              "selected_forecast_q": 70}],
            date_column="week_end_date",
            types={"week_end_date": "DATE"},
        ),
        sales_weekly=[
            {"sales_date": iso(e), "tcin": 100, "location_id": 2750,
             "sale_quantity": 90}
        ],
    )
    resp = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, aggregate="by_sku_week", as_of_date=TODAY,
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["forecast_week_anchor"] == "end"
    assert resp.data["forecast_week_shift_days"] == 0
    rows = resp.data["rows"]
    assert len(rows) == 1
    assert rows[0]["week_end_date"] == e
    assert rows[0]["forecast_units"] == 70.0
    assert rows[0]["actual_units"] == 90.0


# ===========================================================================
# 3. orders_daily latest-state reduction — the non-deterministic-total bug.
# ===========================================================================


#: PO 900 has a same-snapshot receipt-stage tie; PO 901 is a byte-identical
#: duplicate pair; PO 900 also carries a genuinely older snapshot.
#:
#:   PO 900 / tcin 100 / loc 500
#:     - 07-01 snapshot, received 0            <- superseded by snapshot_d DESC
#:     - 07-30 snapshot, received  0, cancel  0   } tie on snapshot_d
#:     - 07-30 snapshot, received 40, cancel 10   } most advanced receipt state
#:     => one surviving line: 100 - 40 - 10 = 50 open
#:   PO 901 / tcin 100 / loc 501
#:     - 07-30 snapshot, received 0, cancel 0  (x2, byte-identical)
#:     => one surviving line: 20 open
#:   tcin 100 total: 70 open across 2 POs, 2 lines.
#:
#: Wrong answers a broken reduction produces: 100+20 = 120 (tie broken toward
#: the pre-receipt row), 150 (no snapshot_d ordering), 40 for PO 901 (duplicate
#: counted twice), 250 (no reduction at all).
_TIED_ORDER_ROWS = [
    {"snapshot_d": "2026-07-01", "purchase_order_id": 900,
     "purchase_order_create_d": "2026-06-01", "tcin": 100,
     "receiving_location_id": 500, "original_order_q": 100,
     "revised_order_q": 100, "item_received_q": 0,
     "cancel_remaining_order_q": 0},
    {"snapshot_d": "2026-07-30", "purchase_order_id": 900,
     "purchase_order_create_d": "2026-06-01", "tcin": 100,
     "receiving_location_id": 500, "original_order_q": 100,
     "revised_order_q": 100, "item_received_q": 0,
     "cancel_remaining_order_q": 0},
    {"snapshot_d": "2026-07-30", "purchase_order_id": 900,
     "purchase_order_create_d": "2026-06-01", "tcin": 100,
     "receiving_location_id": 500, "original_order_q": 100,
     "revised_order_q": 100, "item_received_q": 40,
     "cancel_remaining_order_q": 10},
    {"snapshot_d": "2026-07-30", "purchase_order_id": 901,
     "purchase_order_create_d": "2026-07-01", "tcin": 100,
     "receiving_location_id": 501, "original_order_q": 20,
     "revised_order_q": 20, "item_received_q": 0,
     "cancel_remaining_order_q": 0},
    {"snapshot_d": "2026-07-30", "purchase_order_id": 901,
     "purchase_order_create_d": "2026-07-01", "tcin": 100,
     "receiving_location_id": 501, "original_order_q": 20,
     "revised_order_q": 20, "item_received_q": 0,
     "cancel_remaining_order_q": 0},
]


@pytest.mark.bq
async def test_orders_daily_reduces_to_the_latest_snapshot_per_po_line(
    bq_client: Any, orders_base_types: dict[str, str]
) -> None:
    """Regression: bpd_get_open_orders returned a different headline number on
    every call.

    The base feed ACCUMULATES every daily drop (147,166 rows / 14,160,189 naive
    open units against 7,710 rows / ~498k reduced), so the registry body applies
    `QUALIFY ROW_NUMBER() OVER (PARTITION BY purchase_order_id, tcin,
    receiving_location_id ORDER BY snapshot_d DESC, ...) = 1`. Before the fix
    that ORDER BY was `snapshot_d DESC` alone, which is NOT a total order:
    1,430 groups tie on the latest snapshot_d, BigQuery picked an arbitrary row
    from each, and consecutive uncached runs reported 497,728 / 502,347 /
    504,606 open units.

    The tied rows are receipt-stage duplicates of ONE line (the same drop
    carries a pre-receipt row beside the received row), not distinct lines —
    so the tiebreak picks the most advanced state rather than summing. Summing
    exceeded the line's own revised_order_q in 1,311 of the 1,430 groups.

    This runs production's registry body verbatim over literal rows; only the
    base-table reference is swapped.
    """
    table = orders_latest_state_table(
        _TIED_ORDER_ROWS, types=orders_base_types, marker="single"
    )
    wh = BigQueryWarehouse(client=bq_client, registry={"orders_daily": table})

    # The reduction itself: five input rows collapse to two PO lines.
    _cols, rows = wh.execute_sql(
        "SELECT purchase_order_id, snapshot_d, item_received_q, "
        "cancel_remaining_order_q FROM orders_daily ORDER BY purchase_order_id"
    )
    assert rows == [
        (900, date(2026, 7, 30), 40.0, 10),  # tie broken toward the receipt
        (901, date(2026, 7, 30), 0.0, 0),    # duplicates collapsed to one
    ]

    resp = await get_open_orders(wh, OpenOrdersInput(response_format="json"))
    assert resp.ok is True, resp.error
    assert resp.data["rows"] == [
        {"tcin": 100, "po_count": 2, "open_units": 70.0, "line_count": 2}
    ]


@pytest.mark.bq
async def test_orders_daily_latest_state_is_identical_across_uncached_runs(
    bq_client: Any, orders_base_types: dict[str, str]
) -> None:
    """Determinism, which is the half of the bug a single call cannot catch.

    Each iteration (a) permutes the input rows and (b) varies a comment inside
    the fixture body. The permutation removes any dependence on scan order; the
    comment changes the query text, so BigQuery's results cache — keyed on
    exact text — cannot serve a previous answer. With the total-order tiebreak
    all runs agree; with `ORDER BY snapshot_d DESC` alone they would not.
    """
    seen: list[list[dict[str, Any]]] = []
    order = list(range(len(_TIED_ORDER_ROWS)))
    for run in range(3):
        # Rotate the rows so no run sees the same input sequence.
        permuted = [_TIED_ORDER_ROWS[(i + run) % len(order)] for i in order]
        table = orders_latest_state_table(
            permuted, types=orders_base_types, marker=f"uncached run {run}"
        )
        wh = BigQueryWarehouse(client=bq_client, registry={"orders_daily": table})
        resp = await get_open_orders(wh, OpenOrdersInput(response_format="json"))
        assert resp.ok is True, resp.error
        seen.append(resp.data["rows"])

    assert seen[0] == [{"tcin": 100, "po_count": 2, "open_units": 70.0, "line_count": 2}]
    assert seen[1] == seen[0], f"run 1 disagreed with run 0: {seen[1]} != {seen[0]}"
    assert seen[2] == seen[0], f"run 2 disagreed with run 0: {seen[2]} != {seen[0]}"


@pytest.mark.bq
async def test_orders_daily_reduction_is_not_double_applied_by_the_tool(
    bq_client: Any, orders_base_types: dict[str, str]
) -> None:
    """Exactly one layer owns the latest-state reduction, and it is the registry.

    get_open_orders must add no snapshot filter of its own: an older PO whose
    latest snapshot predates another PO's would vanish from the book if it did.
    """
    rows = [
        # PO 910's newest snapshot is two months older than PO 911's.
        {"snapshot_d": "2026-05-31", "purchase_order_id": 910,
         "purchase_order_create_d": "2026-05-01", "tcin": 100,
         "receiving_location_id": 500, "original_order_q": 30,
         "revised_order_q": 30, "item_received_q": 0,
         "cancel_remaining_order_q": 0},
        {"snapshot_d": "2026-07-30", "purchase_order_id": 911,
         "purchase_order_create_d": "2026-07-01", "tcin": 100,
         "receiving_location_id": 500, "original_order_q": 5,
         "revised_order_q": 5, "item_received_q": 0,
         "cancel_remaining_order_q": 0},
    ]
    table = orders_latest_state_table(
        rows, types=orders_base_types, marker="no global snapshot filter"
    )
    wh = BigQueryWarehouse(client=bq_client, registry={"orders_daily": table})
    resp = await get_open_orders(wh, OpenOrdersInput(response_format="json"))
    assert resp.ok is True, resp.error
    # 35, not 5: a MAX(snapshot_d) filter on top of the per-line reduction
    # would drop PO 910 entirely.
    assert resp.data["rows"] == [
        {"tcin": 100, "po_count": 2, "open_units": 35.0, "line_count": 2}
    ]


# ===========================================================================
# 4. Forecast honesty: drop classification, snapshot policy, coverage.
# ===========================================================================


@pytest.mark.bq
async def test_forecast_drops_are_classified_and_pre_week_never_zero_fills(
    fixture_warehouse: Any,
) -> None:
    """Target ships two structurally different files into one pattern.

    A FORWARD HORIZON drop covers several not-yet-started weeks and publishes
    before them; a WEEKLY RETROSPECTIVE covers one week and publishes after it.
    Reading `max(last_update_d)` as "Target's current forecast" therefore lands
    on a tiny retrospective file — the 10x understatement trap — so each drop
    is labelled.

    And under pre_week, a week whose only forecast was published post-hoc must
    become UNMATCHED, not a fabricated forecast=0 row that reads as a +infinite
    miss.
    """
    b4, b3, b2, b1 = (week_begin(n) for n in (4, 3, 2, 1))
    e4, e3, e2, e1 = (week_end(n) for n in (4, 3, 2, 1))
    forward_snap = b4 - timedelta(days=1)   # published before all three weeks
    retro_snap = b1 + timedelta(days=7)     # published after that week ended

    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [
                {"fiscal_week_begin_d": iso(b4), "tcin": 100, "location_id": 1,
                 "last_update_d": iso(forward_snap), "selected_forecast_q": 60},
                {"fiscal_week_begin_d": iso(b3), "tcin": 100, "location_id": 1,
                 "last_update_d": iso(forward_snap), "selected_forecast_q": 70},
                {"fiscal_week_begin_d": iso(b2), "tcin": 100, "location_id": 1,
                 "last_update_d": iso(forward_snap), "selected_forecast_q": 80},
                {"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
                 "last_update_d": iso(retro_snap), "selected_forecast_q": 40},
            ],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(e4), "tcin": 100, "location_id": 1,
             "sale_quantity": 50},
            {"sales_date": iso(e3), "tcin": 100, "location_id": 1,
             "sale_quantity": 90},
            {"sales_date": iso(e2), "tcin": 100, "location_id": 1,
             "sale_quantity": 75},
            {"sales_date": iso(e1), "tcin": 100, "location_id": 1,
             "sale_quantity": 33},
        ],
    )

    latest = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, aggregate="by_sku_week", response_format="json"
        ),
    )
    pre = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, aggregate="by_sku_week",
            snapshot_policy="pre_week", response_format="json",
        ),
    )
    assert latest.ok and pre.ok, (latest.error, pre.error)

    drops = {d["last_update_d"]: d for d in latest.data["forecast_drops"]}
    assert drops[iso(forward_snap)]["drop_kind"] == "forward_horizon"
    assert drops[iso(forward_snap)]["horizon_weeks"] == 3
    assert drops[iso(retro_snap)]["drop_kind"] == "weekly_retrospective"
    assert drops[iso(retro_snap)]["horizon_weeks"] == 1

    # latest_available: the post-hoc forecast matches its own week's actuals.
    latest_pairs = {(r["tcin"], r["week_end_date"]): r for r in latest.data["rows"]}
    assert latest_pairs[(100, e1)]["forecast_units"] == 40.0
    assert latest.data["coverage"]["matched"]["cells"] == 4

    # pre_week: that week has no pre-week snapshot, so it is actual_only —
    # NOT forecast_units 0.
    pre_pairs = {(r["tcin"], r["week_end_date"]): r for r in pre.data["rows"]}
    assert (100, e1) not in pre_pairs
    assert pre.data["coverage"]["matched"]["cells"] == 3
    assert pre.data["coverage"]["actual_only"]["cells"] == 1
    assert pre.data["coverage"]["actual_only"]["actual_units"] == 33.0
    # The post-hoc drop is visible in the lag stats too.
    assert pre.data["snapshot_lag"]["post_hoc_rows"] == 1


@pytest.mark.bq
async def test_forecast_drop_timing_matches_targets_real_publication_schedule(
    fixture_warehouse: Any,
) -> None:
    """Live-validated timing (Patch #12).

    Target's retrospective weeklies publish EXACTLY 7 days after week-begin,
    and its forward drops publish the MONDAY after the Sunday week-begin
    (begin + 1). The first classifier allowed a +7-day forward tolerance, which
    swallowed the retro pattern whole — all 18 live drops read forward_horizon.
    Retro must therefore be tested first, on the week-END side.
    """
    retro_begin = week_begin(2)
    retro_snap = retro_begin + timedelta(days=7)
    fwd_begin = week_begin(-3)  # a Sunday a few weeks out
    fwd_snap = fwd_begin + timedelta(days=1)  # the Monday after
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [
                {"fiscal_week_begin_d": iso(retro_begin), "tcin": 100,
                 "location_id": 1, "last_update_d": iso(retro_snap),
                 "selected_forecast_q": 30},
                *[
                    {"fiscal_week_begin_d": iso(fwd_begin + timedelta(weeks=k)),
                     "tcin": 100, "location_id": 1,
                     "last_update_d": iso(fwd_snap),
                     "selected_forecast_q": 10 + k}
                    for k in range(3)
                ],
            ],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(week_end(2)), "tcin": 100, "location_id": 1,
             "sale_quantity": 28}
        ],
    )
    resp = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=104, response_format="json")
    )
    assert resp.ok is True, resp.error
    kinds = {d["last_update_d"]: d["drop_kind"] for d in resp.data["forecast_drops"]}
    assert kinds[iso(retro_snap)] == "weekly_retrospective"
    assert kinds[iso(fwd_snap)] == "forward_horizon"


@pytest.mark.bq
async def test_forecast_drops_cap_keeps_the_newest_forty(
    fixture_warehouse: Any,
) -> None:
    """Two review fixes at once.

    (a) A decayed forward drop — per-key overwrites leave a single surviving
    week, published before it — is still forward_horizon, not "anomalous".
    (b) With more than 40 snapshots the NEWEST are kept, so the current forward
    drop (which consumers are told to read) is always in the list. A head slice
    would eventually truncate it away.
    """
    week0 = week_begin(60)  # a Sunday, ~14 months back, inside the 104w cap
    rows = [
        {"fiscal_week_begin_d": iso(week0 + timedelta(weeks=i)), "tcin": 100,
         "location_id": 1,
         "last_update_d": iso(week0 + timedelta(weeks=i) - timedelta(days=2)),
         "selected_forecast_q": 10}
        for i in range(44)
    ]
    horizon_snap = week0 + timedelta(weeks=45)
    rows += [
        {"fiscal_week_begin_d": iso(horizon_snap + timedelta(days=1 + 7 * j)),
         "tcin": 100, "location_id": 1, "last_update_d": iso(horizon_snap),
         "selected_forecast_q": 20}
        for j in range(3)
    ]
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly", rows,
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(week0 + timedelta(days=6)), "tcin": 100,
             "location_id": 1, "sale_quantity": 9}
        ],
    )
    resp = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=104, response_format="json")
    )
    assert resp.ok is True, resp.error
    drops = resp.data["forecast_drops"]
    assert len(drops) == 40
    assert resp.data["forecast_drops_total"] == 45
    kinds = {d["last_update_d"]: d["drop_kind"] for d in drops}
    # The newest, multi-week forward drop survives the cap and is labelled.
    assert kinds[iso(horizon_snap)] == "forward_horizon"
    assert set(kinds.values()) == {"forward_horizon"}


@pytest.mark.bq
async def test_forecast_lag_probe_failure_keeps_the_drop_classification(
    fixture_warehouse: Any,
) -> None:
    """Two best-effort metadata probes, two independent try/excepts.

    A failing snapshot-lag probe must not discard a successful drop
    classification or get misreported as a classification error. The proxy
    below fails only the lag query, identified by its COUNTIF — which is itself
    a port marker: the DuckDB spelling was `COUNT(*) FILTER (WHERE ...)`, a hard
    400 in BigQuery that this very try/except would have degraded silently to
    ok=True.
    """
    b1, e1 = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
              "last_update_d": iso(b1 - timedelta(days=1)),
              "selected_forecast_q": 60}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(e1), "tcin": 100, "location_id": 1,
             "sale_quantity": 50}
        ],
    )

    class _FlakyLag:
        """Delegates everything except the snapshot-lag query, which fails."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def execute_sql(self, sql: str) -> Any:
            if "COUNTIF(" in sql:
                raise RuntimeError("transient failure")
            return self._inner.execute_sql(sql)

    resp = await get_forecast_vs_actual(
        _FlakyLag(wh),
        ForecastVsActualInput(
            weeks_back=104, as_of_date=TODAY, response_format="json"
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["rows"][0]["forecast_units"] == 60.0
    drops = resp.data["forecast_drops"]
    assert drops and all("error" not in d for d in drops), "classification must survive"
    assert resp.data.get("snapshot_lag") is None
    assert "lag probe failed" in resp.data["snapshot_lag_error"]


@pytest.mark.bq
async def test_forecast_dedup_runs_at_the_forecast_tables_own_grain(
    fixture_warehouse: Any,
) -> None:
    """Snapshot dedup partitions by the FORECAST table's grain, independent of
    the spine.

    With per-location forecasts and chain-level sales the spine collapses to
    (tcin, week); partitioning the dedup by the spine too kept ONE arbitrary
    store's forecast, reading 60 (or 40) where the answer is 100.
    """
    b1, e1 = week_begin(1), week_end(1)
    snap = b1 - timedelta(days=2)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [
                {"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
                 "last_update_d": iso(snap), "selected_forecast_q": 60},
                {"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 2,
                 "last_update_d": iso(snap), "selected_forecast_q": 40},
            ],
            date_column="fiscal_week_begin_d",
        ),
        # Chain-level actuals: NO location column at all.
        sales_weekly=[{"sales_date": iso(e1), "tcin": 100, "sale_quantity": 95}],
    )
    resp = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, aggregate="by_sku_week", response_format="json"
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["spine"] == "tcin, week"
    row = resp.data["rows"][0]
    assert row["forecast_units"] == 100.0, "both locations must survive dedup"
    assert row["actual_units"] == 95.0
    assert row["variance_units"] == -5.0
    assert resp.data["coverage"]["matched"]["forecast_units"] == 100.0


@pytest.mark.bq
async def test_forecast_historical_cutoff_requires_a_snapshot_column(
    fixture_warehouse: Any,
) -> None:
    """A cutoff that cannot be enforced is an error, never a silent no-op with
    metadata claiming it was applied."""
    b1, e1 = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
              "selected_forecast_q": 55}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(e1), "tcin": 100, "location_id": 1,
             "sale_quantity": 50}
        ],
    )
    pre = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, snapshot_policy="pre_week", response_format="json"
        ),
    )
    fixed = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, as_of_date=b1, response_format="json"
        ),
    )
    default = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=104, response_format="json")
    )
    assert pre.ok is False and pre.error.code == "SCHEMA_INCOMPATIBLE"
    assert "snapshot" in pre.error.message
    assert fixed.ok is False and fixed.error.code == "SCHEMA_INCOMPATIBLE"
    # Default latest_available still works, and says why there is no cutoff.
    assert default.ok is True, default.error
    assert "no snapshot column" in default.data["snapshot_policy"]
    assert default.data["rows"][0]["forecast_units"] == 55.0


@pytest.mark.bq
async def test_pre_week_min_lead_days_requires_the_pre_week_policy(
    fixture_warehouse: Any,
) -> None:
    """A lead with the wrong policy — or alongside an as_of_date that overrides
    the policy — is a hard error. Silently dropping it would let a forgotten
    `snapshot_policy` read post-hoc revisions as a week-out prediction."""
    b1, e1 = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
              "last_update_d": iso(b1 - timedelta(days=1)),
              "selected_forecast_q": 55}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(e1), "tcin": 100, "location_id": 1,
             "sale_quantity": 50}
        ],
    )
    wrong_policy = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, pre_week_min_lead_days=7, response_format="json"
        ),
    )
    with_as_of = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, snapshot_policy="pre_week",
            pre_week_min_lead_days=7, as_of_date=TODAY, response_format="json",
        ),
    )
    assert wrong_policy.ok is False
    assert wrong_policy.error.code == "INVALID_ARGUMENT"
    assert with_as_of.ok is False
    assert with_as_of.error.code == "INVALID_ARGUMENT"


@pytest.mark.bq
async def test_pre_week_default_lead_excludes_the_monday_after_drop(
    fixture_warehouse: Any,
) -> None:
    """Target never publishes strictly before a week opens — its forward drops
    land the Monday AFTER the Sunday week-begin. So the default lead of 1 day
    excludes the same-week drop by design, and -1 deliberately tolerates it
    (leaking one day of actuals into "pre-week")."""
    b1, e1 = week_begin(1), week_end(1)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": iso(b1), "tcin": 100, "location_id": 1,
              "last_update_d": iso(b1 + timedelta(days=1)),
              "selected_forecast_q": 55}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(e1), "tcin": 100, "location_id": 1,
             "sale_quantity": 50}
        ],
    )
    strict = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, snapshot_policy="pre_week", response_format="json"
        ),
    )
    tolerant = await get_forecast_vs_actual(
        wh,
        ForecastVsActualInput(
            weeks_back=104, snapshot_policy="pre_week",
            pre_week_min_lead_days=-1, response_format="json",
        ),
    )
    assert strict.ok and tolerant.ok, (strict.error, tolerant.error)
    assert strict.data["rows"] == []
    assert strict.data["coverage"]["actual_only"]["cells"] == 1
    assert "snapshot_retention_caveat" in strict.data
    assert tolerant.data["rows"][0]["forecast_units"] == 55.0


@pytest.mark.bq
async def test_forecast_window_with_no_overlap_is_data_unavailable(
    fixture_warehouse: Any,
) -> None:
    """Actuals entirely outside the requested window must be a structured
    error, not an empty table that reads as "zero variance"."""
    future = TODAY + timedelta(days=30)
    wh = fixture_warehouse(
        forecast_weekly=typed_table(
            "forecast_weekly",
            [{"fiscal_week_begin_d": iso(future), "tcin": 100, "location_id": 1,
              "last_update_d": iso(TODAY), "selected_forecast_q": 5}],
            date_column="fiscal_week_begin_d",
        ),
        sales_weekly=[
            {"sales_date": iso(future), "tcin": 100, "location_id": 1,
             "sale_quantity": 9}
        ],
    )
    resp = await get_forecast_vs_actual(
        wh, ForecastVsActualInput(weeks_back=1, response_format="json")
    )
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert "does not overlap" in resp.error.message
    assert resp.error.details["actuals_min"] == iso(future)


# ===========================================================================
# 5. Boundary honesty: partial buckets, inventory staleness, PO-plan snapshots.
# ===========================================================================


@pytest.mark.bq
async def test_sales_summary_flags_partial_buckets_and_reports_its_range(
    fixture_warehouse: Any,
) -> None:
    """A month bucket fed by weekly rows that start on the 30th covers two days
    of that month. It must be flagged, and the response must say what period it
    actually spans."""
    wh = fixture_warehouse(
        sales_weekly=[
            # May: the only week-end lands the 30th.
            {"sales_date": "2026-05-30", "tcin": 100, "location_id": 1,
             "sale_quantity": 10, "sale_amount": 30.0},
            # June: a full month of weekly rows.
            {"sales_date": "2026-06-06", "tcin": 100, "location_id": 1,
             "sale_quantity": 20, "sale_amount": 60.0},
            {"sales_date": "2026-06-13", "tcin": 100, "location_id": 1,
             "sale_quantity": 20, "sale_amount": 60.0},
            {"sales_date": "2026-06-20", "tcin": 100, "location_id": 1,
             "sale_quantity": 20, "sale_amount": 60.0},
            {"sales_date": "2026-06-27", "tcin": 100, "location_id": 1,
             "sale_quantity": 20, "sale_amount": 60.0},
        ]
    )
    resp = await get_sales_summary(
        wh, SalesSummaryInput(grain="month", response_format="json")
    )
    assert resp.ok is True, resp.error
    rows = {r["bucket"]: r for r in resp.data["rows"]}
    assert rows[date(2026, 5, 1)]["total_units"] == 10.0
    assert rows[date(2026, 5, 1)]["partial_bucket"] is True, (
        "May's data starts 29 days into the bucket"
    )
    assert rows[date(2026, 6, 1)]["total_units"] == 80.0
    assert rows[date(2026, 6, 1)]["partial_bucket"] is False
    assert resp.data["effective_start"] == "2026-05-30"
    assert resp.data["effective_end"] == "2026-06-27"
    assert resp.data["source_grain"] == "week"
    assert "week_straddle_note" in resp.data
    assert resp.data["alternative_source"] is None  # no sales_daily registered


@pytest.mark.bq
async def test_inventory_snapshot_reports_and_filters_staleness(
    fixture_warehouse: Any,
) -> None:
    """"Latest known per pair" silently carries rows across feed gaps — 2,178
    stale pairs in production. The carry must be counted, and excludable."""
    wh = fixture_warehouse(
        inventory_daily=[
            {"business_d": "2026-07-30", "tcin": 100, "location_id": 500,
             "ending_on_hand_q": 8},   # current
            {"business_d": "2026-05-19", "tcin": 100, "location_id": 501,
             "ending_on_hand_q": 44},  # last seen 72 days earlier
        ]
    )
    resp = await get_inventory_snapshot(
        wh, InventorySnapshotInput(response_format="json")
    )
    assert resp.ok is True, resp.error
    st = resp.data["staleness"]
    assert st["window_max_date"] == "2026-07-30"
    # Per-pair staleness and feed lag are different numbers, both surfaced.
    assert st["feed_lag_days_vs_as_of"] == (TODAY - date(2026, 7, 30)).days
    assert st["returned_pairs"] == 2
    assert st["stale_pairs_over_7d"] == 1
    assert st["max_staleness_days_returned"] == 72
    assert "note" in st

    filtered = await get_inventory_snapshot(
        wh, InventorySnapshotInput(max_staleness_days=7, response_format="json")
    )
    rows = filtered.data["rows"]
    assert [(r["location_id"], r["on_hand"]) for r in rows] == [(500, 8)], (
        "max_staleness_days=7 must exclude the 72-day-old pair"
    )


@pytest.mark.bq
async def test_inventory_staleness_anchors_to_the_as_of_window(
    fixture_warehouse: Any,
) -> None:
    """Staleness is measured against the feed's newest date WITHIN the as_of
    window. Anchored to the whole-table max instead, a historical as_of plus
    max_staleness_days returned zero rows."""
    wh = fixture_warehouse(
        inventory_daily=[
            {"business_d": "2026-07-30", "tcin": 100, "location_id": 500,
             "ending_on_hand_q": 8},
            {"business_d": "2026-05-19", "tcin": 100, "location_id": 501,
             "ending_on_hand_q": 44},
        ]
    )
    resp = await get_inventory_snapshot(
        wh,
        InventorySnapshotInput(
            as_of=date(2026, 5, 25), max_staleness_days=7, response_format="json"
        ),
    )
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    # Inside the window the 05-19 snapshot IS the freshest, so it is returned —
    # not filtered against the out-of-window 07-30 maximum.
    assert [(r["location_id"], r["on_hand"]) for r in rows] == [(501, 44)]
    assert resp.data["staleness"]["window_max_date"] == "2026-05-19"


@pytest.mark.bq
async def test_sell_through_staleness_filter_excludes_rather_than_fabricates(
    fixture_warehouse: Any,
) -> None:
    """A pair dropped by max_staleness_days must vanish.

    Under the old LEFT JOIN it came back with on_hand NULL, which the ratio
    read as "fully sold through" (1.0) — the exact opposite of an overstocked
    pair whose inventory is a month stale.
    """
    wh = fixture_warehouse(
        sales_weekly=[
            {"sales_date": "2026-07-25", "tcin": 1, "location_id": 5,
             "sale_quantity": 40},
            {"sales_date": "2026-07-25", "tcin": 2, "location_id": 5,
             "sale_quantity": 10},
        ],
        inventory_daily=[
            # tcin 1: 29 days stale and heavily overstocked.
            {"business_d": "2026-07-01", "tcin": 1, "location_id": 5,
             "ending_on_hand_q": 500},
            # tcin 2: current.
            {"business_d": "2026-07-30", "tcin": 2, "location_id": 5,
             "ending_on_hand_q": 60},
        ],
    )
    resp = await get_sell_through(
        wh, SellThroughInput(max_staleness_days=7, response_format="json")
    )
    assert resp.ok is True, resp.error
    rows = {r["tcin"]: r for r in resp.data["rows"]}
    assert 1 not in rows, (
        "the stale overstocked pair must be excluded, not reported as 100% "
        "sold through"
    )
    assert rows[2]["on_hand"] == 60
    assert rows[2]["sell_through_rate"] == pytest.approx(10 / 70)


@pytest.mark.bq
async def test_upcoming_pos_present_but_empty_is_data_unavailable(
    fixture_warehouse: Any,
) -> None:
    """A registered-but-empty po_plan table is a data-availability state, not
    SCHEMA_INCOMPATIBLE — the health smoke test treats DATA_UNAVAILABLE as a
    benign skip and anything else as breakage."""
    populated = fixture_table(
        "po_plan_daily",
        [{"business_d": iso(TODAY), "tcin": 100, "order_d": iso(TODAY),
          "receiving_location_id": 500, "ordered_q": 1}],
        date_column="business_d",
    )
    wh = fixture_warehouse(po_plan_daily=empty_like(populated))
    resp = await get_upcoming_pos(wh, UpcomingPosInput(response_format="json"))
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert "po_plan_daily" in resp.error.message
    assert resp.error.details["empty_tables"] == ["po_plan_daily"]


@pytest.mark.bq
async def test_upcoming_pos_uncastable_snapshot_does_not_abort_the_tool(
    fixture_warehouse: Any,
) -> None:
    """One table's bad date value must not take the whole tool down.

    po_plan_biweekly here ships `business_d` as STRING holding Target's literal
    `""` placeholder. Two things are pinned: the snapshot probe uses SAFE_CAST
    (a plain CAST is a hard 400 that would fail the whole call), and the table
    degrades to `empty_tables` while the healthy daily plan still returns.
    """
    fresh = iso(TODAY - timedelta(days=1))
    in_window = iso(TODAY + timedelta(days=3))
    wh = fixture_warehouse(
        po_plan_daily=[
            {"business_d": fresh, "tcin": 100, "order_d": in_window,
             "receiving_location_id": 500, "ordered_q": 40}
        ],
        po_plan_biweekly=typed_table(
            "po_plan_biweekly",
            [{"business_d": '""', "tcin": 100, "order_d": in_window,
              "receiving_location_id": 500, "ordered_q": 60}],
            date_column="business_d",
            types={"business_d": "STRING"},
        ),
    )
    resp = await get_upcoming_pos(wh, UpcomingPosInput(response_format="json"))
    assert resp.ok is True, resp.error
    assert resp.data["source_totals"] == {"po_plan_daily": 40}
    assert resp.data["empty_tables"] == ["po_plan_biweekly"]
    assert "po_plan_biweekly" not in resp.data["resolved_columns"]


@pytest.mark.bq
async def test_upcoming_pos_reports_snapshot_ages_and_flags_divergence(
    fixture_warehouse: Any,
) -> None:
    """The 07-29/07-31 launch-buy incident, encoded.

    When the two plans' snapshots straddle more than a day, POs cut in the gap
    show as planned units in the older plan and as firm orders in the newer
    one — so the plans can legitimately disagree, and the response says so.
    """
    in_window = iso(TODAY + timedelta(days=3))
    wh = fixture_warehouse(
        po_plan_daily=[
            {"business_d": iso(TODAY - timedelta(days=1)), "tcin": 100,
             "order_d": in_window, "receiving_location_id": 500,
             "ordered_q": 40}
        ],
        po_plan_biweekly=[
            {"business_d": iso(TODAY - timedelta(days=4)), "tcin": 100,
             "order_d": in_window, "receiving_location_id": 500,
             "ordered_q": 500}
        ],
    )
    resp = await get_upcoming_pos(
        wh, UpcomingPosInput(weeks_forward=8, response_format="json")
    )
    assert resp.ok is True, resp.error
    rc = resp.data["resolved_columns"]
    # Tolerate a midnight straddle between fixture setup and the tool's
    # date.today(); the divergence is exact because both ages shift together.
    assert rc["po_plan_daily"]["snapshot_age_days"] in (1, 2)
    assert rc["po_plan_biweekly"]["snapshot_age_days"] in (4, 5)
    assert resp.data["snapshot_divergence_days"] == 3
    assert "po_plan_daily for the near horizon" in resp.data["divergence_note"]
