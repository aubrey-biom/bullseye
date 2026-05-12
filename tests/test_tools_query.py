"""Verify the math of bpd_get_sales_summary on a small fixture, and that the
response_format toggle yields the expected shape."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from bpd_mcp.schemas import (
    DescribeSchemaInput,
    InventorySnapshotInput,
    SalesSummaryInput,
    TopSkusInput,
)
from bpd_mcp.tools.query import (
    describe_schema,
    get_inventory_snapshot,
    get_sales_summary,
    get_top_skus,
)
from bpd_mcp.warehouse import Warehouse


def _seed_warehouse(path: Path) -> None:
    """Build a tiny sales_weekly + inventory_weekly table for math tests."""
    rw = Warehouse(path / "bpd.duckdb")
    rw.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "week_end_date DATE, units_sold BIGINT, sales_dollars DOUBLE)"
    )
    rw.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(1, 100, DATE '2026-04-25', 50, 100.0), "
        "(1, 200, DATE '2026-04-25', 30, 60.0), "
        "(2, 100, DATE '2026-04-25', 20, 80.0), "
        "(1, 100, DATE '2026-04-18', 40, 80.0), "
        "(2, 100, DATE '2026-04-18', 10, 40.0)"
    )
    rw.execute_sql(
        "CREATE TABLE inventory_weekly (tcin BIGINT, location_id BIGINT, "
        "week_end_date DATE, on_hand_units BIGINT)"
    )
    rw.execute_sql(
        "INSERT INTO inventory_weekly VALUES "
        "(1, 100, DATE '2026-04-25', 200), "
        "(1, 200, DATE '2026-04-25', 50), "
        "(2, 100, DATE '2026-04-25', 75)"
    )
    rw.close()


async def test_sales_summary_weekly_math(tmp_path: Path) -> None:
    _seed_warehouse(tmp_path)
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        # No filters: sum all 5 rows by week — week of 4/25 has 100u/240$, week of 4/18 has 50u/120$.
        resp = await get_sales_summary(
            ro, SalesSummaryInput(grain="week", response_format="markdown")
        )
        assert resp.ok is True
        rows = resp.data["rows"]
        totals_by_units = {r["bucket"]: r["total_units"] for r in rows}
        # date_trunc('week', ...) lands on the Monday of that week.
        assert sum(totals_by_units.values()) == 150
        totals_by_dollars = {r["bucket"]: r["total_dollars"] for r in rows}
        assert sum(totals_by_dollars.values()) == pytest.approx(360.0)

        # Filter to one TCIN.
        resp2 = await get_sales_summary(
            ro,
            SalesSummaryInput(grain="week", tcin=1, response_format="json"),
        )
        assert resp2.format == "json"
        rows2 = resp2.data["rows"]
        assert sum(r["total_units"] for r in rows2) == 120  # 50+30+40
    finally:
        ro.close()


async def test_sales_summary_format_toggle(tmp_path: Path) -> None:
    _seed_warehouse(tmp_path)
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        md = await get_sales_summary(
            ro, SalesSummaryInput(grain="week", response_format="markdown")
        )
        js = await get_sales_summary(ro, SalesSummaryInput(grain="week", response_format="json"))
        assert md.format == "markdown"
        assert "|" in md.rendered  # markdown table
        assert js.format == "json"
        # JSON output should round-trip.
        import json as _json

        parsed = _json.loads(js.rendered)
        assert "rows" in parsed
    finally:
        ro.close()


async def test_top_skus_orders_by_metric(tmp_path: Path) -> None:
    _seed_warehouse(tmp_path)
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        resp = await get_top_skus(
            ro,
            TopSkusInput(by="units", top_n=10, response_format="json"),
        )
        rows = resp.data["rows"]
        # TCIN 1 sold 50+30+40 = 120, TCIN 2 sold 20+10 = 30.
        assert rows[0]["tcin"] == 1
        assert rows[0]["metric_total"] == 120

        resp2 = await get_top_skus(
            ro,
            TopSkusInput(by="dollars", top_n=10, response_format="json"),
        )
        rows2 = resp2.data["rows"]
        # Dollars: TCIN 1 = 100+60+80 = 240, TCIN 2 = 80+40 = 120.
        assert rows2[0]["tcin"] == 1
        assert rows2[0]["metric_total"] == pytest.approx(240.0)
    finally:
        ro.close()


async def test_inventory_snapshot_picks_latest(tmp_path: Path) -> None:
    rw = Warehouse(tmp_path / "bpd.duckdb")
    rw.execute_sql(
        "CREATE TABLE inventory_daily (tcin BIGINT, location_id BIGINT, "
        "snapshot_date DATE, on_hand_units BIGINT)"
    )
    rw.execute_sql(
        "INSERT INTO inventory_daily VALUES "
        "(1, 100, DATE '2026-04-21', 50), "
        "(1, 100, DATE '2026-04-22', 45), "
        "(1, 100, DATE '2026-04-23', 40), "
        "(1, 200, DATE '2026-04-22', 99)"
    )
    rw.close()
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        resp = await get_inventory_snapshot(
            ro,
            InventorySnapshotInput(as_of=date(2026, 4, 23), response_format="json"),
        )
        rows = resp.data["rows"]
        # Two (tcin, location) pairs, each with its latest snapshot.
        by_loc = {(r["tcin"], r["location_id"]): r["on_hand"] for r in rows}
        assert by_loc[(1, 100)] == 40  # 4/23 latest
        assert by_loc[(1, 200)] == 99  # only one row, 4/22
        # Bound: as_of = 4/22 should clip to 45 / 99.
        resp2 = await get_inventory_snapshot(
            ro,
            InventorySnapshotInput(as_of=date(2026, 4, 22), response_format="json"),
        )
        by_loc2 = {(r["tcin"], r["location_id"]): r["on_hand"] for r in resp2.data["rows"]}
        assert by_loc2[(1, 100)] == 45
        assert by_loc2[(1, 200)] == 99
    finally:
        ro.close()


async def test_describe_schema_returns_loaded_tables(tmp_path: Path) -> None:
    _seed_warehouse(tmp_path)
    ro = Warehouse(tmp_path / "bpd.duckdb", read_only=True)
    try:
        resp = await describe_schema(ro, DescribeSchemaInput(response_format="markdown"))
        assert "sales_weekly" in resp.rendered
        assert "inventory_weekly" in resp.rendered
        # Schema info also present in `data` for JSON consumers.
        resp_j = await describe_schema(ro, DescribeSchemaInput(response_format="json"))
        assert "sales_weekly" in resp_j.data["tables"]
    finally:
        ro.close()
