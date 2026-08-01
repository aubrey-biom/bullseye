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
        "(100, 2750, '2026-05-04', DATE '2026-05-01', 55), "  # pre-week prediction
        "(100, 2750, '2026-05-04', DATE '2026-05-12', 48), "  # post-week revised
        "(200, 2750, '2026-05-04', DATE '2026-05-01', 10)"
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
    # Actual units for tcin 100 in sales_weekly: 50 + 30 = 80
    assert by_tcin[100]["actual_units"] == 80
    # variance_units = 80 - 55 = 25; variance_pct = 25/55 ≈ 0.4545
    assert by_tcin[100]["variance_units"] == 25
    assert abs(by_tcin[100]["variance_pct"] - 25 / 55) < 1e-6


async def test_get_forecast_vs_actual_default_as_of_picks_pre_week(tmp_path: Path) -> None:
    """When as_of_date is None, default cutoff = (week_start - 1 day)."""
    wh = _seed_real_columns(tmp_path)
    ro = ReadOnlyView(wh)
    try:
        resp = await get_forecast_vs_actual(
            ro,
            ForecastVsActualInput(
                weeks_back=104, aggregate="by_sku", response_format="json"
            ),
        )
    finally:
        wh.close()
    assert resp.ok is True, resp.error
    # Pre-week cutoff (2026-05-03) → only the 5/1 snapshot is eligible.
    rows = {r["tcin"]: r for r in resp.data["rows"]}
    assert rows[100]["forecast_units"] == 55
    # Extra reports as_of_date used
    assert "pre-week" in resp.data["as_of_date_used"]


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
    # variance_pct = -10/60 ≈ -0.1667
    assert abs(by_pair[(100, date(2026, 4, 25))]["variance_pct"] - (-10 / 60)) < 1e-6

    assert by_pair[(100, date(2026, 5, 2))]["forecast_units"] == 70
    assert by_pair[(100, date(2026, 5, 2))]["actual_units"] == 90
    assert by_pair[(100, date(2026, 5, 2))]["variance_units"] == 20

    assert by_pair[(100, date(2026, 5, 9))]["forecast_units"] == 80
    assert by_pair[(100, date(2026, 5, 9))]["actual_units"] == 75
    assert by_pair[(100, date(2026, 5, 9))]["variance_units"] == -5

    # Forecast-only: tcin 999 on week ending 5/9 — forecast 12, actual 0.
    fc_only = by_pair.get((999, date(2026, 5, 9)))
    assert fc_only is not None
    assert fc_only["forecast_units"] == 12
    assert fc_only["actual_units"] == 0

    # Actual-only: tcin 888 on week ending 5/2 — forecast 0, actual 5.
    act_only = by_pair.get((888, date(2026, 5, 2)))
    assert act_only is not None
    assert act_only["forecast_units"] == 0
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
