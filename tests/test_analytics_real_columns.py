"""Analytics-tool tests against Target's *real* column names (Issue 1 + Issue 6).

Earlier tests used idealized names like `units_sold` and `week_end_date`. After
patch #4, the tools use the column-role registry and must work against the names
Target actually ships: `sale_quantity`, `sales_date`, `selected_forecast_q`,
`fiscal_week_begin_d`, etc.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from bpd_mcp.schemas import (
    ForecastVsActualInput,
    InventorySnapshotInput,
    SalesSummaryInput,
    SellThroughInput,
    TopSkusInput,
)
from bpd_mcp.tools.query import (
    get_forecast_vs_actual,
    get_inventory_snapshot,
    get_sales_summary,
    get_sell_through,
    get_top_skus,
)
from bpd_mcp.warehouse import ReadOnlyView, Warehouse


def _seed_real_columns(path: Path) -> Warehouse:
    """Build a warehouse using the names Target actually ships in real BPD files."""
    wh = Warehouse(path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE sales_daily ("
        "tcin BIGINT, location_id BIGINT, sales_date DATE, "
        "sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    wh.execute_sql(
        "INSERT INTO sales_daily VALUES "
        "(100, 2750, DATE '2026-05-04', 10, 30.00), "
        "(100, 3275, DATE '2026-05-04', 7, 21.00), "
        "(200, 2750, DATE '2026-05-04', 3, 12.00), "
        "(100, 2750, DATE '2026-05-05', 5, 15.00)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly ("
        "tcin BIGINT, location_id BIGINT, sales_date DATE, "
        "sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(100, 2750, DATE '2026-05-09', 50, 150.00), "
        "(100, 3275, DATE '2026-05-09', 30, 90.00), "
        "(200, 2750, DATE '2026-05-09', 12, 48.00)"
    )
    wh.execute_sql(
        "CREATE TABLE inventory_daily ("
        "tcin BIGINT, location_id BIGINT, report_date_dim DATE, "
        "inventory_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        "(100, 2750, DATE '2026-05-04', 200), "
        "(100, 2750, DATE '2026-05-05', 195), "
        "(100, 3275, DATE '2026-05-05', 150), "
        "(200, 2750, DATE '2026-05-05', 75)"
    )
    # Forecast: VARCHAR fiscal_week_begin_d + DATE last_update_d.
    wh.execute_sql(
        "CREATE TABLE forecast_weekly ("
        "tcin BIGINT, location_id BIGINT, fiscal_week_begin_d VARCHAR, "
        "last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        # Two snapshots of the same forecast week — different last_update_d.
        # Week begins Sunday 2026-05-03 → canonical Saturday week-end 05-09,
        # aligning with sales_weekly's sales_date above.
        "(100, 2750, '2026-05-03', DATE '2026-05-01', 55), "  # pre-week prediction
        "(100, 2750, '2026-05-03', DATE '2026-05-12', 48), "  # post-week revised
        "(200, 2750, '2026-05-03', DATE '2026-05-01', 10)"
    )
    return wh


async def test_get_sales_summary_works_with_real_column_names(tmp_path: Path) -> None:
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_sales_summary(
            ro, SalesSummaryInput(grain="day", response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    by_bucket = {r["bucket"]: r for r in rows}
    # 5/4: 10 + 7 + 3 = 20; 5/5: 5
    assert by_bucket[date(2026, 5, 4)]["total_units"] == 20
    assert by_bucket[date(2026, 5, 5)]["total_units"] == 5
    # dollars too
    assert by_bucket[date(2026, 5, 4)]["total_dollars"] == 63.0
    # extra reports resolved column names
    assert resp.data["units_col"] == "sale_quantity"
    assert resp.data["date_col"] == "sales_date"


async def test_get_top_skus_works_with_real_column_names(tmp_path: Path) -> None:
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_top_skus(
            ro,
            TopSkusInput(by="units", top_n=10, response_format="json"),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    # TCIN 100 = 50 + 30 = 80; TCIN 200 = 12 → TCIN 100 first.
    assert rows[0]["tcin"] == 100
    assert rows[0]["metric_total"] == 80
    # Patch #12 pins: a no-arg call must say what period it spans, and point
    # at the other grain's coverage.
    assert resp.data["effective_start"] == "2026-05-09"
    assert resp.data["effective_end"] == "2026-05-09"
    assert resp.data["alternative_source"]["table"] == "sales_daily"
    assert resp.data["alternative_source"]["min_date"] == "2026-05-04"
    assert resp.data["alternative_source"]["scope"] == "entire table, unfiltered"


async def test_get_inventory_snapshot_works_with_real_column_names(tmp_path: Path) -> None:
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_inventory_snapshot(
            ro,
            InventorySnapshotInput(as_of=date(2026, 5, 5), response_format="json"),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    by_pair = {(r["tcin"], r["location_id"]): r for r in rows}
    # Latest per (tcin, location).
    assert by_pair[(100, 2750)]["on_hand"] == 195
    assert by_pair[(100, 3275)]["on_hand"] == 150
    assert by_pair[(200, 2750)]["on_hand"] == 75


async def test_get_forecast_vs_actual_works_with_real_column_names(tmp_path: Path) -> None:
    """Patch #11 semantics: coverage-honest spine. Only MATCHED (tcin, location,
    week) cells produce variance — tcin 100's loc-3275 actuals have no forecast
    and are counted in extra.coverage instead of inflating actual_units."""
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        # weeks_back is large enough to cover the seed dates regardless of today.
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                as_of_date=date(2026, 5, 3),  # cutoff before the post-week revision
                aggregate="by_sku",
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    by_tcin = {r["tcin"]: r for r in rows}
    # With as_of_date=2026-05-03 we use the 5/1 snapshot (55) not the 5/12 one (48).
    assert by_tcin[100]["forecast_units"] == 55
    # Matched cell only: loc 2750 actuals (50). Loc 3275's 30 units have no
    # forecast — they are actual_only coverage, NOT silently added.
    assert by_tcin[100]["actual_units"] == 50
    assert by_tcin[100]["variance_units"] == -5
    # variance_pct is now a true percent: 100 * -5 / 55 ≈ -9.09
    assert abs(by_tcin[100]["variance_pct"] - (100 * -5 / 55)) < 1e-6
    assert by_tcin[200]["forecast_units"] == 10
    assert by_tcin[200]["actual_units"] == 12
    cov = resp.data["coverage"]
    assert cov["matched"]["cells"] == 2
    assert cov["actual_only"]["cells"] == 1
    assert cov["actual_only"]["actual_units"] == 30


async def test_get_forecast_vs_actual_snapshot_policy(tmp_path: Path) -> None:
    """Patch #11: default policy is latest_available (per-key retention keeps
    one snapshot anyway); pre_week recovers the old pre-week-prediction pick."""
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        latest = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, aggregate="by_sku", response_format="json"
            ),
        )
        pre_week = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                aggregate="by_sku",
                snapshot_policy="pre_week",
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert latest.ok and pre_week.ok
    latest_rows = {r["tcin"]: r for r in latest.data["rows"]}
    pre_rows = {r["tcin"]: r for r in pre_week.data["rows"]}
    # latest_available → the 5/12 post-week revision (48) wins.
    assert latest_rows[100]["forecast_units"] == 48
    assert "latest_available" in latest.data["snapshot_policy"]
    # pre_week → only the 5/1 snapshot (55) is eligible (published before 5/03).
    assert pre_rows[100]["forecast_units"] == 55
    assert "pre_week" in pre_week.data["snapshot_policy"]


async def test_get_forecast_vs_actual_diagnostic_error_when_column_missing(
    tmp_path: Path,
) -> None:
    """Error must include dataset, role, candidates, actual_columns (brief Issue 1)."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    # forecast_weekly with a non-canonical units column name that's NOT in the
    # registry's candidate list.
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, fiscal_week_begin_d DATE, "
        "weird_column_name_for_units BIGINT)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, sales_date DATE, sale_quantity BIGINT)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro, ForecastVsActualInput(weeks_back=8, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is False
    assert resp.error.code == "SCHEMA_INCOMPATIBLE"
    detail = resp.error.details
    assert detail["dataset"] == "forecast_weekly"
    assert detail["role"] == "units"
    assert "selected_forecast_q" in detail["candidates"]
    assert "weird_column_name_for_units" in detail["actual_columns"]


async def test_analytics_sees_new_table_without_mcp_restart(tmp_path: Path) -> None:
    """Issue 6 regression: a table created AFTER the warehouse is opened must be
    visible to analytics tools immediately, without restarting the MCP."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    ro = ReadOnlyView(wh)
    try:
        # First call: table does not exist.
        resp1 = await get_sales_summary(
            ro, SalesSummaryInput(grain="week", response_format="json")
        )
        assert resp1.ok is False
        assert resp1.error.code == "DATA_UNAVAILABLE"

        # Now create the table (simulates a sync). NO MCP restart, NO reconnect.
        wh.execute_sql(
            "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
            "sales_date DATE, sale_quantity BIGINT)"
        )
        wh.execute_sql(
            "INSERT INTO sales_weekly VALUES (100, 2750, DATE '2026-05-09', 50)"
        )

        # Second call: must succeed and see the new table.
        resp2 = await get_sales_summary(
            ro, SalesSummaryInput(grain="week", response_format="json")
        )
        assert resp2.ok is True, resp2.error
        assert resp2.data["rows"][0]["total_units"] == 50
    finally:
        wh.close()


async def test_get_sell_through_uses_resolved_columns(tmp_path: Path) -> None:
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_sell_through(
            ro, SellThroughInput(response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    extra = resp.data
    assert extra["resolved_columns"]["sales_units"] == "sale_quantity"
    assert extra["resolved_columns"]["sales_date"] == "sales_date"
    assert extra["resolved_columns"]["inv_on_hand"] == "inventory_quantity"


# ---------- Patch #5: Sunday/Saturday week-anchor join ----------


def _seed_sunday_saturday_pairs(path: Path) -> Warehouse:
    """Fixture exercising the Patch #5 bug.

    forecast_weekly uses Sunday-anchored fiscal_week_begin_d, sales_weekly uses
    Saturday-anchored sales_date. The +6 day shift inside get_forecast_vs_actual
    must canonicalize both sides to Saturday so the FULL OUTER JOIN finds the
    matched (tcin, week) pairs.
    """
    wh = Warehouse(path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly ("
        "tcin BIGINT, location_id BIGINT, fiscal_week_begin_d VARCHAR, "
        "last_update_d DATE, selected_forecast_q BIGINT)"
    )
    # Sundays for forecasts. Each Sunday + 6 days = the corresponding Saturday.
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        # tcin 100: forecasts for three weeks. Snapshot before each week begins
        # (last_update_d = the Saturday immediately before the Sunday).
        "(100, 2750, '2026-04-19', DATE '2026-04-18', 60), "
        "(100, 2750, '2026-04-26', DATE '2026-04-25', 70), "
        "(100, 2750, '2026-05-03', DATE '2026-05-02', 80), "
        # tcin 999: forecast-only — no matching actuals.
        "(999, 2750, '2026-05-03', DATE '2026-05-02', 12)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly ("
        "tcin BIGINT, location_id BIGINT, sales_date DATE, sale_quantity BIGINT)"
    )
    # Saturdays = Sundays + 6 days.
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(100, 2750, DATE '2026-04-25', 50), "
        "(100, 2750, DATE '2026-05-02', 90), "
        "(100, 2750, DATE '2026-05-09', 75), "
        # tcin 888: actual-only — no matching forecast.
        "(888, 2750, DATE '2026-05-02', 5)"
    )
    return wh


async def test_forecast_vs_actual_canonicalizes_sunday_to_saturday(
    tmp_path: Path,
) -> None:
    """The original bug: zero matched rows because Sundays and Saturdays never met.

    After the fix:
      * tcin 100 has THREE matched (tcin, week_end_date) rows with both fc>0 and act>0
      * tcin 999 (forecast-only) shows up with actual_units = 0
      * tcin 888 (actual-only) shows up with forecast_units = 0
    """
    wh = _seed_sunday_saturday_pairs(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,  # ~10 years — independent of "today"
                aggregate="by_sku_week",
                # Use an explicit cutoff that includes all our seeded snapshots.
                as_of_date=date(2026, 5, 12),
                response_format="json",
            ),
        )
    finally:
        wh.close()

    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    matched = [r for r in rows if r["forecast_units"] > 0 and r["actual_units"] > 0]

    # Regression assertion: pre-fix this would have been 0.
    assert len(matched) >= 1, (
        "expected at least one matched row with both forecast_units > 0 AND "
        "actual_units > 0; pre-fix this was 0 because the Sunday/Saturday anchor "
        "mismatch made the FULL OUTER JOIN find nothing."
    )

    # tcin 100 should have three matched weeks (4/25, 5/2, 5/9 Saturday week-ends).
    by_pair = {(r["tcin"], r["week_end_date"]): r for r in rows}
    assert by_pair[(100, date(2026, 4, 25))]["forecast_units"] == 60
    assert by_pair[(100, date(2026, 4, 25))]["actual_units"] == 50
    # variance_units = actual - forecast = 50 - 60 = -10
    assert by_pair[(100, date(2026, 4, 25))]["variance_units"] == -10
    # variance_pct is a true percent (Patch #11): 100 * -10/60 ≈ -16.67
    assert abs(by_pair[(100, date(2026, 4, 25))]["variance_pct"] - (100 * -10 / 60)) < 1e-6

    assert by_pair[(100, date(2026, 5, 2))]["forecast_units"] == 70
    assert by_pair[(100, date(2026, 5, 2))]["actual_units"] == 90
    assert by_pair[(100, date(2026, 5, 2))]["variance_units"] == 20

    assert by_pair[(100, date(2026, 5, 9))]["forecast_units"] == 80
    assert by_pair[(100, date(2026, 5, 9))]["actual_units"] == 75
    assert by_pair[(100, date(2026, 5, 9))]["variance_units"] == -5

    # Patch #11: unmatched cells are EXCLUDED from the default output — never
    # zero-filled into fake variances — and counted in extra.coverage instead.
    assert (999, date(2026, 5, 9)) not in by_pair, "forecast-only row must not appear"
    assert (888, date(2026, 5, 2)) not in by_pair, "actual-only row must not appear"
    cov = resp.data["coverage"]
    assert cov["forecast_only"] == {"cells": 1, "forecast_units": 12, "actual_units": None}
    assert cov["actual_only"] == {"cells": 1, "forecast_units": None, "actual_units": 5}
    assert cov["matched"]["cells"] == 3

    # include_unmatched=true returns them with the missing side NULL (not 0).
    wh2 = _seed_sunday_saturday_pairs(tmp_path / "again")
    ro2 = ReadOnlyView(wh2)
    try:
        resp2 = await get_forecast_vs_actual(
            ro2,
            ForecastVsActualInput(
                weeks_back=104,
                aggregate="by_sku_week",
                as_of_date=date(2026, 5, 12),
                include_unmatched=True,
                response_format="json",
            ),
        )
    finally:
        wh2.close()
    by_pair2 = {(r["tcin"], r["week_end_date"]): r for r in resp2.data["rows"]}
    fc_only = by_pair2[(999, date(2026, 5, 9))]
    assert fc_only["coverage"] == "forecast_only"
    assert fc_only["forecast_units"] == 12
    assert fc_only["actual_units"] is None
    assert fc_only["variance_pct"] is None
    act_only = by_pair2[(888, date(2026, 5, 2))]
    assert act_only["coverage"] == "actual_only"
    assert act_only["forecast_units"] is None
    assert act_only["actual_units"] == 5

    # Extra surfaces the shift so future-debugging is one tool call away.
    assert resp.data["forecast_week_anchor"] == "begin"
    assert resp.data["forecast_week_shift_days"] == 6


async def test_forecast_vs_actual_no_shift_when_column_is_week_end(
    tmp_path: Path,
) -> None:
    """If Target ships forecast_weekly with a week-END date column (rather than
    fiscal_week_begin_d), the +6 day shift must NOT apply — the column already
    aligns with sales_weekly's Saturday anchor.
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        # Note: week_end_date is the resolved name; the +6 must NOT fire.
        "CREATE TABLE forecast_weekly ("
        "tcin BIGINT, location_id BIGINT, week_end_date DATE, "
        "last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        "(100, 2750, DATE '2026-05-02', DATE '2026-04-25', 70)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly ("
        "tcin BIGINT, location_id BIGINT, sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES (100, 2750, DATE '2026-05-02', 90)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                aggregate="by_sku_week",
                as_of_date=date(2026, 5, 12),
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    assert resp.data["forecast_week_anchor"] == "end"
    assert resp.data["forecast_week_shift_days"] == 0
    # Match should still happen because both sides use 2026-05-02.
    matched = [
        r for r in resp.data["rows"]
        if r["forecast_units"] > 0 and r["actual_units"] > 0
    ]
    assert len(matched) == 1
    assert matched[0]["tcin"] == 100
    assert matched[0]["week_end_date"] == date(2026, 5, 2)


# --------- Patch #10: rewritten S&OP tools against real Target columns ---------


def _seed_orders_and_plans(path: Path):
    """orders_daily + po_plan_* with REAL Target column names.

    Deliberately includes the columns the old (deleted) local candidate tuples
    could never resolve — these tests fail on any regression to that path.
    """
    from datetime import timedelta

    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE orders_daily ("
        "snapshot_d DATE, purchase_order_id VARCHAR, "
        "purchase_order_create_d DATE, tcin BIGINT, "
        "receiving_location_id BIGINT, original_order_q BIGINT, "
        "revised_order_q BIGINT, item_received_q BIGINT, "
        "cancel_remaining_order_q BIGINT, purchase_order_active_f BOOLEAN)"
    )
    wh.execute_sql(
        "INSERT INTO orders_daily VALUES "
        # PO-1 line 1: open = 100 - 40 - 10 = 50
        "(DATE '2026-07-30', 'PO-1', DATE '2026-06-01', 100, 500, 100, 100, 40, 10, NULL), "
        # PO-1 line 2: fully received → open 0 → excluded
        "(DATE '2026-07-30', 'PO-1', DATE '2026-06-01', 200, 500, 30, 30, 30, 0, NULL), "
        # PO-2: NULL received/cancel treated as 0 → open 20
        "(DATE '2026-07-30', 'PO-2', DATE '2026-07-01', 100, 501, 20, 20, NULL, NULL, NULL), "
        # PO-3: over-received → negative → excluded
        "(DATE '2026-07-30', 'PO-3', DATE '2026-06-15', 300, 500, 15, 15, 20, 0, NULL)"
    )

    today = date.today()
    fresh = (today - timedelta(days=1)).isoformat()
    stale = (today - timedelta(days=2)).isoformat()
    in_window = (today + timedelta(days=3)).isoformat()
    out_window = (today + timedelta(weeks=20)).isoformat()
    for table in ("po_plan_daily", "po_plan_biweekly"):
        wh.execute_sql(
            f"CREATE TABLE {table} ("
            "business_d DATE, tcin BIGINT, order_d DATE, "
            "receiving_location_id BIGINT, ordered_q BIGINT)"
        )
    wh.execute_sql(
        "INSERT INTO po_plan_daily VALUES "
        # STALE snapshot — must be invisible to the tool.
        f"(DATE '{stale}', 100, DATE '{in_window}', 500, 999), "
        # Latest snapshot: 40 + 60 units inside the window...
        f"(DATE '{fresh}', 100, DATE '{in_window}', 500, 40), "
        f"(DATE '{fresh}', 100, DATE '{in_window}', 501, 60), "
        # ...and 77 outside the 8-week window.
        f"(DATE '{fresh}', 100, DATE '{out_window}', 500, 77)"
    )
    wh.execute_sql(
        "INSERT INTO po_plan_biweekly VALUES "
        f"(DATE '{fresh}', 100, DATE '{in_window}', 500, 500)"
    )
    return wh


async def test_get_open_orders_derives_open_units_from_latest_state(
    tmp_path: Path,
) -> None:
    from bpd_mcp.schemas import OpenOrdersInput
    from bpd_mcp.tools.query import get_open_orders

    wh = _seed_orders_and_plans(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_open_orders(ro, OpenOrdersInput(response_format="json"))
        assert resp.ok is True, resp.error
        rows = {r["tcin"]: r for r in resp.data["rows"]}
        # tcin 100: PO-1 (50 open) + PO-2 (20 open) = 70 across 2 POs.
        assert rows[100]["open_units"] == 70
        assert rows[100]["po_count"] == 2
        assert rows[100]["line_count"] == 2
        # Fully-received and over-received lines never appear.
        assert 200 not in rows
        assert 300 not in rows
        assert "revised_order_q" in resp.data["method"]

        # as_of_date = PO CREATION cutoff (not time travel): only PO-1 (6/01)
        # and PO-3 (6/15) qualify at 6/15; PO-3 has nothing open.
        resp2 = await get_open_orders(
            ro,
            OpenOrdersInput(as_of_date=date(2026, 6, 15), response_format="json"),
        )
        rows2 = {r["tcin"]: r for r in resp2.data["rows"]}
        assert rows2[100]["open_units"] == 50
        assert rows2[100]["po_count"] == 1

        # location_filter routes through receiving_location_id.
        resp3 = await get_open_orders(
            ro, OpenOrdersInput(location_filter=[501], response_format="json")
        )
        rows3 = {r["tcin"]: r for r in resp3.data["rows"]}
        assert rows3[100]["open_units"] == 20
    finally:
        wh.close()


async def test_get_upcoming_pos_uses_latest_snapshot_and_splits_sources(
    tmp_path: Path,
) -> None:
    """The two Patch-#10 landmines: (1) without the max(business_d) filter the
    stale snapshot's 999 units would double-count; (2) without per-source
    grouping the daily 100 and biweekly 500 would blend into one 600."""
    from bpd_mcp.schemas import UpcomingPosInput
    from bpd_mcp.tools.query import get_upcoming_pos

    wh = _seed_orders_and_plans(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_upcoming_pos(
            ro, UpcomingPosInput(weeks_forward=8, response_format="json")
        )
        assert resp.ok is True, resp.error
        rows = resp.data["rows"]
        by_source = {}
        for r in rows:
            by_source.setdefault(r["source"], 0)
            by_source[r["source"]] += r["planned_units"]
        # Only the LATEST snapshot counts: 40+60, not 999, not 77 (outside window).
        assert by_source == {"po_plan_daily": 100, "po_plan_biweekly": 500}
        assert resp.data["source_totals"] == {
            "po_plan_daily": 100,
            "po_plan_biweekly": 500,
        }
        resolved = resp.data["resolved_columns"]
        assert resolved["po_plan_daily"]["qty_col"] == "ordered_q"
        assert resolved["po_plan_daily"]["order_date_col"] == "order_d"
        assert resolved["po_plan_daily"]["snapshot_col"] == "business_d"
    finally:
        wh.close()


async def test_inventory_tools_work_with_real_inventory_daily_columns(
    tmp_path: Path,
) -> None:
    """P0-1 regression guard: inventory_daily with the REAL on-hand column
    (ending_on_hand_q) must resolve — this exact shape hard-failed before."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE inventory_daily ("
        "business_d DATE, tcin BIGINT, location_id BIGINT, "
        "beginning_on_hand_q BIGINT, ending_on_hand_q BIGINT, "
        "ending_on_transfer_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        "(DATE '2026-07-01', 100, 500, 10, 8, 1), "
        "(DATE '2026-07-02', 100, 500, 8, 5, 0)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_inventory_snapshot(
            ro, InventorySnapshotInput(response_format="json")
        )
        assert resp.ok is True, resp.error
        assert resp.data["on_hand_col"] == "ending_on_hand_q"
        rows = resp.data["rows"]
        assert len(rows) == 1
        # Latest day's ENDING on-hand, not the beginning_ bookend (10).
        assert rows[0]["on_hand"] == 5
    finally:
        wh.close()


async def test_get_upcoming_pos_empty_tables_are_data_unavailable(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: a present-but-empty po_plan table is a
    data-availability state, not SCHEMA_INCOMPATIBLE — the health smoke test
    treats DATA_UNAVAILABLE as a benign skip, anything else as breakage."""
    from bpd_mcp.schemas import UpcomingPosInput
    from bpd_mcp.tools.query import get_upcoming_pos
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE po_plan_daily ("
        "business_d DATE, tcin BIGINT, order_d DATE, "
        "receiving_location_id BIGINT, ordered_q BIGINT)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_upcoming_pos(ro, UpcomingPosInput(response_format="json"))
        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == "DATA_UNAVAILABLE"
        assert "po_plan_daily" in resp.error.message
    finally:
        wh.close()


async def test_get_upcoming_pos_bad_date_value_degrades_to_skipped_table(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: an uncastable date value in ONE table must not
    abort the tool — the healthy table still returns, the broken one lands in
    extra.skipped_tables."""
    from datetime import timedelta

    from bpd_mcp.schemas import UpcomingPosInput
    from bpd_mcp.tools.query import get_upcoming_pos
    from bpd_mcp.warehouse import Warehouse

    today = date.today()
    fresh = (today - timedelta(days=1)).isoformat()
    in_window = (today + timedelta(days=3)).isoformat()

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE po_plan_daily ("
        "business_d DATE, tcin BIGINT, order_d DATE, "
        "receiving_location_id BIGINT, ordered_q BIGINT)"
    )
    wh.execute_sql(
        f"INSERT INTO po_plan_daily VALUES (DATE '{fresh}', 100, DATE '{in_window}', 500, 40)"
    )
    # VARCHAR business_d with a value that cannot CAST to DATE.
    wh.execute_sql(
        "CREATE TABLE po_plan_biweekly ("
        "business_d VARCHAR, tcin BIGINT, order_d DATE, "
        "receiving_location_id BIGINT, ordered_q BIGINT)"
    )
    wh.execute_sql(
        f"INSERT INTO po_plan_biweekly VALUES ('bogus', 100, DATE '{in_window}', 500, 60)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_upcoming_pos(ro, UpcomingPosInput(response_format="json"))
        assert resp.ok is True, resp.error
        assert resp.data["source_totals"] == {"po_plan_daily": 40}
        assert "po_plan_biweekly" in resp.data["skipped_tables"]
        assert "probe failed" in resp.data["skipped_tables"]["po_plan_biweekly"]
    finally:
        wh.close()


# --------- Patch #11: forecast_vs_actual honesty features ---------


async def test_forecast_window_clamps_to_actuals_coverage(tmp_path: Path) -> None:
    """A 104-week ask over ~3 weeks of actuals reports its effective range
    instead of silently truncating (4e)."""
    wh = _seed_sunday_saturday_pairs(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                aggregate="by_sku_week",
                as_of_date=date(2026, 5, 12),
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    assert resp.data["requested_weeks_back"] == 104
    assert resp.data["effective_start"] == "2026-04-25"
    assert resp.data["effective_end"] == "2026-05-09"
    assert resp.data["effective_weeks_covered"] == 3
    assert resp.data["window_truncated_to_actuals_coverage"] is True


async def test_forecast_no_overlap_returns_data_unavailable(tmp_path: Path) -> None:
    """Actuals entirely outside the requested window → structured error, not
    an empty table that reads as 'zero variance'."""
    from datetime import timedelta

    from bpd_mcp.warehouse import Warehouse

    future = (date.today() + timedelta(days=30)).isoformat()
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        f"INSERT INTO forecast_weekly VALUES (100, 1, '{future}', DATE '{future}', 5)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        f"INSERT INTO sales_weekly VALUES (100, 1, DATE '{future}', 9)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro, ForecastVsActualInput(weeks_back=1, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert "does not overlap" in resp.error.message


async def test_forecast_drops_classified_and_pre_week_never_zero_fills(
    tmp_path: Path,
) -> None:
    """4a + 4b together. forecast_weekly holds a forward-horizon drop and a
    post-hoc retrospective drop. extra.forecast_drops labels both. Under
    pre_week, a week whose forecast exists ONLY post-hoc becomes unmatched —
    not a fabricated forecast=0 row; under latest_available it matches."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        # Forward-horizon drop: 3 weeks published 04-18, before any of them.
        "(100, 1, '2026-04-19', DATE '2026-04-18', 60), "
        "(100, 1, '2026-04-26', DATE '2026-04-18', 70), "
        "(100, 1, '2026-05-03', DATE '2026-04-18', 80), "
        # Retrospective drop: week 05-10 published 06-01 — AFTER the week.
        "(100, 1, '2026-05-10', DATE '2026-06-01', 40)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(100, 1, DATE '2026-04-25', 50), "
        "(100, 1, DATE '2026-05-02', 90), "
        "(100, 1, DATE '2026-05-09', 75), "
        "(100, 1, DATE '2026-05-16', 33)"   # week whose forecast is post-hoc only
    )
    ro = ReadOnlyView(wh)
    try:
        latest = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, aggregate="by_sku_week", response_format="json"
            ),
        )
        pre = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                aggregate="by_sku_week",
                snapshot_policy="pre_week",
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert latest.ok and pre.ok, (latest.error, pre.error)

    # Drop classification (4b).
    drops = {d["last_update_d"]: d for d in latest.data["forecast_drops"]}
    assert drops["2026-04-18"]["drop_kind"] == "forward_horizon"
    assert drops["2026-04-18"]["horizon_weeks"] == 3
    assert drops["2026-06-01"]["drop_kind"] == "weekly_retrospective"

    # latest_available: the post-hoc 05-16 forecast matches its actuals.
    latest_pairs = {(r["tcin"], str(r["week_end_date"])): r for r in latest.data["rows"]}
    assert latest_pairs[(100, "2026-05-16")]["forecast_units"] == 40

    # pre_week: that week's only snapshot is post-hoc → unmatched, NOT zero.
    pre_pairs = {(r["tcin"], str(r["week_end_date"])): r for r in pre.data["rows"]}
    assert (100, "2026-05-16") not in pre_pairs
    assert pre.data["coverage"]["actual_only"]["cells"] == 1
    assert pre.data["coverage"]["actual_only"]["actual_units"] == 33
    # The three forward-horizon weeks still match under pre_week.
    assert pre.data["coverage"]["matched"]["cells"] == 3
    # Snapshot-lag honesty: the post-hoc drop is visible in the lag stats.
    assert pre.data["snapshot_lag"]["post_hoc_rows"] == 1


async def test_forecast_dedup_keeps_all_locations_when_sales_is_chain_level(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix (critical): snapshot dedup must run at the
    FORECAST table's own grain. With per-location forecasts and chain-level
    sales, the dedup partition previously collapsed to (tcin, week) and kept
    ONE arbitrary store's forecast — here that read 60 (or 40) instead of 100."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        "(100, 1, '2026-05-03', DATE '2026-05-01', 60), "
        "(100, 2, '2026-05-03', DATE '2026-05-01', 40)"
    )
    # Chain-level sales: NO location column.
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql("INSERT INTO sales_weekly VALUES (100, DATE '2026-05-09', 95)")
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, aggregate="by_sku_week", response_format="json"
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    assert resp.data["spine"] == "tcin, week"
    row = resp.data["rows"][0]
    assert row["forecast_units"] == 100, "both locations' forecasts must survive dedup"
    assert row["actual_units"] == 95
    assert row["variance_units"] == -5
    assert resp.data["coverage"]["matched"]["forecast_units"] == 100


async def test_forecast_snapshot_cutoff_requires_snapshot_column(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: pre_week / as_of_date against a forecast table
    with no snapshot column must ERROR, not silently no-op while extra claims
    the cutoff was applied."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES (100, 1, '2026-05-03', 55)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES (100, 1, DATE '2026-05-09', 50)"
    )
    ro = ReadOnlyView(wh)
    try:
        pre = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, snapshot_policy="pre_week", response_format="json"
            ),
        )
        fixed = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, as_of_date=date(2026, 4, 1), response_format="json"
            ),
        )
        default = await get_forecast_vs_actual(
            ro, ForecastVsActualInput(weeks_back=104, response_format="json")
        )
    finally:
        wh.close()
    assert pre.ok is False and pre.error.code == "SCHEMA_INCOMPATIBLE"
    assert "snapshot" in pre.error.message
    assert fixed.ok is False and fixed.error.code == "SCHEMA_INCOMPATIBLE"
    # Default latest_available works and says so honestly.
    assert default.ok is True, default.error
    assert "no snapshot column" in default.data["snapshot_policy"]


async def test_forecast_drops_forward_labels_survive_decay_and_cap_keeps_newest(
    tmp_path: Path,
) -> None:
    """Adversarial-review fixes: (a) a decayed forward drop (single surviving
    week, published before it) and a genuine one-week-ahead forward drop are
    forward_horizon, not 'anomalous'; (b) with >40 snapshots the NEWEST are
    kept, so the current forward_horizon drop is always visible."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    # 44 decayed weekly forward residues: snapshot each Friday before its one
    # surviving week (Sunday), Jan 2025 onward.
    rows = []
    from datetime import timedelta

    week0 = date(2025, 1, 5)  # a Sunday
    for i in range(44):
        wk = week0 + timedelta(weeks=i)
        snap = wk - timedelta(days=2)
        rows.append(f"(100, 1, '{wk.isoformat()}', DATE '{snap.isoformat()}', 10)")
    # Newest drop: a genuine 3-week forward horizon.
    horizon_snap = week0 + timedelta(weeks=45)
    for j in range(3):
        wk = horizon_snap + timedelta(days=1 + 7 * j)
        rows.append(
            f"(100, 1, '{wk.isoformat()}', DATE '{horizon_snap.isoformat()}', 20)"
        )
    wh.execute_sql("INSERT INTO forecast_weekly VALUES " + ", ".join(rows))
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        f"INSERT INTO sales_weekly VALUES "
        f"(100, 1, DATE '{(week0 + timedelta(days=6)).isoformat()}', 9)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro, ForecastVsActualInput(weeks_back=104, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    drops = resp.data["forecast_drops"]
    assert len(drops) == 40
    assert resp.data["forecast_drops_total"] == 45
    kinds = {d["last_update_d"]: d["drop_kind"] for d in drops}
    # The newest (multi-week forward) drop MUST be present and labeled forward.
    assert kinds[horizon_snap.isoformat()] == "forward_horizon"
    # Decayed single-week forward residues are forward_horizon, not anomalous.
    residue_kinds = {
        k for d, k in kinds.items() if d != horizon_snap.isoformat()
    }
    assert residue_kinds == {"forward_horizon"}


async def test_forecast_lag_probe_failure_keeps_drop_classification(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: a failing snapshot-lag probe must not discard a
    successful drop classification or misattribute the error."""

    wh = _seed_sunday_saturday_pairs(tmp_path)

    class _FlakyLag:
        """Proxy that fails only the lag query (FILTER (WHERE ...))."""

        def __init__(self, inner: Warehouse) -> None:
            self._inner = inner

        @property
        def read_only(self) -> bool:
            return self._inner.read_only

        def execute_sql(self, sql: str):
            if "FILTER (WHERE" in sql:
                raise RuntimeError("transient lock contention")
            return self._inner.execute_sql(sql)

    try:
        resp = await get_forecast_vs_actual(
            _FlakyLag(wh),
            ForecastVsActualInput(
                weeks_back=104,
                as_of_date=date(2026, 5, 12),
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    drops = resp.data["forecast_drops"]
    assert all("error" not in d for d in drops), "classification must survive"
    assert resp.data.get("snapshot_lag") is None
    assert "lag probe failed" in resp.data["snapshot_lag_error"]


# --------- Patch #12: live-validated classifier timing + honesty extras ---------


async def test_forecast_drops_real_target_publication_timing(tmp_path: Path) -> None:
    """Live-validated fix: Target's retro weeklies publish EXACTLY 7 days after
    week-begin and its forward drops publish the MONDAY after the Sunday
    week-begin. The first classifier's +7d forward tolerance swallowed every
    retro drop (all 18 live drops read forward_horizon)."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        # Retro weekly: week begins Sun 04-19, publishes Sun 04-26 (= begin+7).
        "(100, 1, '2026-04-19', DATE '2026-04-26', 30), "
        # Forward drop: weeks begin Sun 07-19..08-02, publishes Mon 07-20 (= min+1).
        "(100, 1, '2026-07-19', DATE '2026-07-20', 10), "
        "(100, 1, '2026-07-26', DATE '2026-07-20', 11), "
        "(100, 1, '2026-08-02', DATE '2026-07-20', 12)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES (100, 1, DATE '2026-04-25', 28)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro, ForecastVsActualInput(weeks_back=104, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    kinds = {d["last_update_d"]: d["drop_kind"] for d in resp.data["forecast_drops"]}
    assert kinds["2026-04-26"] == "weekly_retrospective"
    assert kinds["2026-07-20"] == "forward_horizon"


async def test_pre_week_min_lead_days_matches_publication_reality(
    tmp_path: Path,
) -> None:
    """Target never publishes strictly before the week opens (Monday-after
    drops), so default pre_week excludes the same-week drop — and
    pre_week_min_lead_days=-1 deliberately tolerates it."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, selected_forecast_q BIGINT)"
    )
    # Week begins Sun 2026-05-03; snapshot publishes Mon 2026-05-04 (begin+1).
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        "(100, 1, '2026-05-03', DATE '2026-05-04', 55)"
    )
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES (100, 1, DATE '2026-05-09', 50)"
    )
    ro = ReadOnlyView(wh)
    try:
        strict = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, snapshot_policy="pre_week", response_format="json"
            ),
        )
        tolerant = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                snapshot_policy="pre_week",
                pre_week_min_lead_days=-1,
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert strict.ok and tolerant.ok
    # Strict: the Monday-after snapshot fails the cutoff → week is actual_only.
    assert strict.data["rows"] == []
    assert strict.data["coverage"]["actual_only"]["cells"] == 1
    assert "snapshot_retention_caveat" in strict.data
    # Tolerant (-1 day): the same-week Monday drop is accepted.
    assert tolerant.data["rows"][0]["forecast_units"] == 55


async def test_sales_summary_partial_buckets_and_effective_range(
    tmp_path: Path,
) -> None:
    """A month bucket fed by weekly rows starting mid-month must be flagged
    partial, and the response must say what period it actually covers."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        # May: first week-end lands the 30th — only two days of May covered.
        "(100, 1, DATE '2026-05-30', 10, 30.0), "
        # June: full month of weekly rows.
        "(100, 1, DATE '2026-06-06', 20, 60.0), "
        "(100, 1, DATE '2026-06-13', 20, 60.0), "
        "(100, 1, DATE '2026-06-20', 20, 60.0), "
        "(100, 1, DATE '2026-06-27', 20, 60.0)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_sales_summary(
            ro, SalesSummaryInput(grain="month", response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    assert rows[0]["partial_bucket"] is True, "May starts 29 days into the bucket"
    assert rows[-1]["partial_bucket"] is False or len(rows) == 1
    assert resp.data["effective_start"] == "2026-05-30"
    assert resp.data["effective_end"] == "2026-06-27"
    assert resp.data["source_grain"] == "week"
    assert "week_straddle_note" in resp.data
    assert resp.data["alternative_source"] is None  # no sales_daily seeded


async def test_inventory_snapshot_staleness_reporting_and_filter(
    tmp_path: Path,
) -> None:
    """The 2,178-stale-pairs problem: 'latest known' carried across feed gaps
    must be counted, and max_staleness_days must exclude it on request."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE inventory_daily (business_d DATE, tcin BIGINT, "
        "location_id BIGINT, ending_on_hand_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        "(DATE '2026-07-30', 100, 500, 8), "   # current
        "(DATE '2026-05-19', 100, 501, 44)"     # stale: last seen 72 days earlier
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_inventory_snapshot(
            ro, InventorySnapshotInput(response_format="json")
        )
        filtered = await get_inventory_snapshot(
            ro,
            InventorySnapshotInput(max_staleness_days=7, response_format="json"),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    st = resp.data["staleness"]
    assert st["window_max_date"] == "2026-07-30"
    assert st["returned_pairs"] == 2
    assert st["stale_pairs_over_7d"] == 1
    assert st["max_staleness_days_returned"] == 72
    assert "note" in st

    rows = filtered.data["rows"]
    assert len(rows) == 1 and rows[0]["location_id"] == 500, (
        "max_staleness_days=7 must exclude the 72-day-old pair"
    )


async def test_upcoming_pos_snapshot_ages_and_divergence_flag(
    tmp_path: Path,
) -> None:
    """The 07-29/07-31 launch-buy incident, encoded: per-source snapshot age
    plus an explicit divergence note when the two plans straddle >1 day."""
    from datetime import timedelta

    from bpd_mcp.schemas import UpcomingPosInput
    from bpd_mcp.tools.query import get_upcoming_pos
    from bpd_mcp.warehouse import Warehouse

    today = date.today()
    in_window = (today + timedelta(days=3)).isoformat()
    daily_snap = (today - timedelta(days=1)).isoformat()
    biweekly_snap = (today - timedelta(days=4)).isoformat()

    wh = Warehouse(tmp_path / "bpd.duckdb")
    for table, snap, qty in (
        ("po_plan_daily", daily_snap, 40),
        ("po_plan_biweekly", biweekly_snap, 500),
    ):
        wh.execute_sql(
            f"CREATE TABLE {table} (business_d DATE, tcin BIGINT, order_d DATE, "
            "receiving_location_id BIGINT, ordered_q BIGINT)"
        )
        wh.execute_sql(
            f"INSERT INTO {table} VALUES "
            f"(DATE '{snap}', 100, DATE '{in_window}', 500, {qty})"
        )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_upcoming_pos(
            ro, UpcomingPosInput(weeks_forward=8, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rc = resp.data["resolved_columns"]
    # Tolerate a midnight straddle between fixture setup and the tool's
    # date.today(); divergence is exact because both ages shift together.
    assert rc["po_plan_daily"]["snapshot_age_days"] in (1, 2)
    assert rc["po_plan_biweekly"]["snapshot_age_days"] in (4, 5)
    assert resp.data["snapshot_divergence_days"] == 3
    assert "po_plan_daily for the near horizon" in resp.data["divergence_note"]


async def test_open_orders_surfaces_over_received_lines(tmp_path: Path) -> None:
    """Over-received lines (received + cancel > ordered) are excluded from open
    units — correctly — but must be a labeled count, not a silent filter."""
    from bpd_mcp.schemas import OpenOrdersInput
    from bpd_mcp.tools.query import get_open_orders

    wh = _seed_orders_and_plans(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_open_orders(ro, OpenOrdersInput(response_format="json"))
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    # PO-3 in the fixture: ordered 15, received 20 → open -5.
    assert resp.data["over_received"] == {"lines": 1, "units_over": 5}


async def test_inventory_staleness_anchors_to_as_of_window(tmp_path: Path) -> None:
    """Adversarial-review fix (major): staleness is measured against the feed's
    newest date WITHIN the as_of window — anchored to the whole-table max, a
    historical as_of + max_staleness_days returned zero rows."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE inventory_daily (business_d DATE, tcin BIGINT, "
        "location_id BIGINT, ending_on_hand_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        "(DATE '2026-07-30', 100, 500, 8), "
        "(DATE '2026-05-19', 100, 501, 44)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_inventory_snapshot(
            ro,
            InventorySnapshotInput(
                as_of=date(2026, 5, 25),
                max_staleness_days=7,
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = resp.data["rows"]
    # Within the as_of window the 05-19 snapshot IS the freshest — it must
    # be returned, not filtered against the (out-of-window) 07-30 max.
    assert len(rows) == 1 and rows[0]["location_id"] == 501
    assert resp.data["staleness"]["window_max_date"] == "2026-05-19"


async def test_sell_through_staleness_filter_excludes_not_fabricates(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix (major): a stale pair filtered by
    max_staleness_days must be EXCLUDED — under the old LEFT JOIN it came back
    as on_hand NULL → sell_through_rate 1.0 ('fully sold through')."""
    from bpd_mcp.warehouse import Warehouse

    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(1, 5, DATE '2026-07-25', 40), "
        "(2, 5, DATE '2026-07-25', 10)"
    )
    wh.execute_sql(
        "CREATE TABLE inventory_daily (business_d DATE, tcin BIGINT, "
        "location_id BIGINT, ending_on_hand_q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        # tcin 1's inventory is 30 days stale and heavily overstocked.
        "(DATE '2026-07-01', 1, 5, 500), "
        # tcin 2's is current.
        "(DATE '2026-07-30', 2, 5, 60)"
    )
    ro = ReadOnlyView(wh)
    try:
        resp = await get_sell_through(
            ro, SellThroughInput(max_staleness_days=7, response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    rows = {r["tcin"]: r for r in resp.data["rows"]}
    assert 1 not in rows, (
        "the stale overstocked pair must be excluded, not reported as 100% "
        "sold through"
    )
    assert rows[2]["on_hand"] == 60


async def test_pre_week_min_lead_days_requires_pre_week_policy(
    tmp_path: Path,
) -> None:
    """Adversarial-review fix: a non-default lead with the wrong policy (or an
    as_of_date override) is a hard error, never a silent no-op."""
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        wrong_policy = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, pre_week_min_lead_days=7, response_format="json"
            ),
        )
        with_as_of = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104,
                snapshot_policy="pre_week",
                pre_week_min_lead_days=7,
                as_of_date=date(2026, 5, 3),
                response_format="json",
            ),
        )
    finally:
        wh.close()
    assert wrong_policy.ok is False
    assert wrong_policy.error.code == "INVALID_ARGUMENT"
    assert with_as_of.ok is False
    assert with_as_of.error.code == "INVALID_ARGUMENT"
