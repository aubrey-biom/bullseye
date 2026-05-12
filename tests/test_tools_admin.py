"""Admin tool tests — focused on the patch-2 fix for cache_status date detection."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from bpd_mcp.schemas import CacheStatusInput
from bpd_mcp.tools.admin import cache_status
from bpd_mcp.warehouse import Warehouse


def _seed_warehouse_with_dates(path: Path) -> None:
    """Build a warehouse with one dataset that has a typed DATE column and one
    that has a TEXT date column, plus one with no date column at all."""
    wh = Warehouse(path / "bpd.duckdb")
    # Typed DATE column.
    wh.execute_sql(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "week_end_date DATE, units_sold BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(1, 100, DATE '2026-03-01', 10), (1, 100, DATE '2026-05-01', 20)"
    )
    # TEXT date column — Target sometimes ships dates as strings. detect_date_column
    # should still discover it via the column-name heuristic.
    wh.execute_sql(
        "CREATE TABLE orders_daily (tcin BIGINT, location_id BIGINT, "
        "order_date TEXT, open_units BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO orders_daily VALUES "
        "(1, 100, '2026-04-15', 5), (2, 100, '2026-04-20', 3)"
    )
    # No date column at all.
    wh.execute_sql(
        "CREATE TABLE item_attr (tcin BIGINT, description TEXT, brand TEXT)"
    )
    wh.execute_sql("INSERT INTO item_attr VALUES (1, 'foo', 'biom'), (2, 'bar', 'biom')")
    wh.close()


@pytest.fixture()
def settings_with_db(tmp_path: Path):
    from pydantic import SecretStr

    from bpd_mcp.config import Settings

    s = Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        bpd_data_dir=str(tmp_path),
    )
    s.ensure_dirs()
    return s


async def test_cache_status_returns_non_null_date_range(
    tmp_path: Path, settings_with_db
) -> None:
    """The patch-2 fix: bpd_cache_status must return real min/max dates."""
    _seed_warehouse_with_dates(tmp_path)
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        resp = await cache_status(
            wh, settings_with_db, CacheStatusInput(response_format="json")
        )
    finally:
        wh.close()

    assert resp.ok is True
    data = resp.data
    # Overall range is non-null when any dataset has a date column.
    assert data["earliest_data_date"] is not None
    assert data["latest_data_date"] is not None
    # And the range covers what we inserted (DATE comparable to date literal).
    assert data["earliest_data_date"] <= date(2026, 3, 1)
    assert data["latest_data_date"] >= date(2026, 5, 1)


async def test_cache_status_per_dataset_breakdown(
    tmp_path: Path, settings_with_db
) -> None:
    """Per-dataset min/max + detected date_column appear in the response."""
    _seed_warehouse_with_dates(tmp_path)
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        resp = await cache_status(
            wh, settings_with_db, CacheStatusInput(response_format="json")
        )
    finally:
        wh.close()

    by_dataset = {row["dataset"]: row for row in resp.data["per_dataset"]}
    # The typed DATE column dataset.
    assert "sales_weekly" in by_dataset
    assert by_dataset["sales_weekly"]["date_column"] == "week_end_date"
    assert by_dataset["sales_weekly"]["row_count"] == 2
    # The TEXT-dated dataset still gets a detected column (name-heuristic fallback).
    assert "orders_daily" in by_dataset
    assert by_dataset["orders_daily"]["date_column"] == "order_date"
    # The dataset with no date column gets date_column=None gracefully.
    assert "item_attr" in by_dataset
    assert by_dataset["item_attr"]["date_column"] is None
    assert by_dataset["item_attr"]["min_date"] is None
    assert by_dataset["item_attr"]["max_date"] is None


async def test_cache_status_handles_empty_warehouse(
    tmp_path: Path, settings_with_db
) -> None:
    """When no datasets are loaded yet, earliest/latest are null but the call doesn't crash."""
    wh = Warehouse(tmp_path / "bpd.duckdb")  # bare warehouse, no data tables
    try:
        resp = await cache_status(
            wh, settings_with_db, CacheStatusInput(response_format="json")
        )
    finally:
        wh.close()
    assert resp.ok is True
    assert resp.data["earliest_data_date"] is None
    assert resp.data["latest_data_date"] is None
    assert resp.data["per_dataset"] == []
