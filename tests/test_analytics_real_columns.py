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
