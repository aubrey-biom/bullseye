"""Column-role registry tests — Issue 1 + Issue 6 (call-time resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bpd_mcp.column_roles import (
    COLUMN_ROLES,
    DATASET_KINDS,
    ColumnNotFound,
    ResolvedColumn,
    resolve_column,
    table_exists,
)
from bpd_mcp.warehouse import Warehouse


def test_resolve_column_finds_target_real_world_names(tmp_path: Path) -> None:
    """The bug: real Target schemas use `sale_quantity`, `selected_forecast_q`,
    `fiscal_week_begin_d`. The registry must catch them.
    """
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE sales_daily (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
        "fiscal_week_begin_d VARCHAR, last_update_d DATE, "
        "selected_forecast_q BIGINT)"
    )
    try:
        # sales_daily
        date_col = resolve_column(wh, "sales_daily", "date")
        assert date_col.name == "sales_date"
        units_col = resolve_column(wh, "sales_daily", "units")
        assert units_col.name == "sale_quantity"
        dollars_col = resolve_column(wh, "sales_daily", "dollars")
        assert dollars_col.name == "sale_amount"
        # forecast_weekly
        fc_date = resolve_column(wh, "forecast_weekly", "date")
        assert fc_date.name == "fiscal_week_begin_d"
        # fiscal_week_begin_d is VARCHAR — the cast must apply.
        assert not fc_date.is_date_typed
        assert "CAST(" in fc_date.select_as_date()
        fc_units = resolve_column(wh, "forecast_weekly", "units")
        assert fc_units.name == "selected_forecast_q"
        fc_snap = resolve_column(wh, "forecast_weekly", "snapshot_date")
        assert fc_snap.name == "last_update_d"
    finally:
        wh.close()


def test_resolve_column_missing_raises_with_diagnostic_detail(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE sales_daily (tcin BIGINT, some_other_col TEXT)")
    try:
        with pytest.raises(ColumnNotFound) as ei:
            resolve_column(wh, "sales_daily", "units")
        detail = ei.value.detail
        assert detail["dataset"] == "sales_daily"
        assert detail["role"] == "units"
        assert "sale_quantity" in detail["candidates"]
        assert detail["actual_columns"] == ["tcin", "some_other_col"]
    finally:
        wh.close()


def test_resolve_column_picks_first_match_in_order(tmp_path: Path) -> None:
    """When multiple candidates are present, the first one in the registry wins."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    # sale_quantity is FIRST in the units candidates; units_sold is second.
    wh.execute_sql(
        "CREATE TABLE sales_daily (tcin BIGINT, sale_quantity BIGINT, units_sold BIGINT)"
    )
    try:
        col = resolve_column(wh, "sales_daily", "units")
        assert col.name == "sale_quantity"
    finally:
        wh.close()


def test_resolve_column_extra_candidates(tmp_path: Path) -> None:
    """Caller-supplied extra candidates are appended (lowest priority)."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql("CREATE TABLE sales_daily (tcin BIGINT, my_custom_units BIGINT)")
    try:
        col = resolve_column(
            wh, "sales_daily", "units", extra_candidates=("my_custom_units",)
        )
        assert col.name == "my_custom_units"
    finally:
        wh.close()


def test_table_exists_introspects_fresh_at_call_time(tmp_path: Path) -> None:
    """Issue 6 invariant: table_exists must see post-sync schema with no caching."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        assert table_exists(wh, "sales_daily") is False
        wh.execute_sql("CREATE TABLE sales_daily (tcin BIGINT)")
        # No restart, no cache invalidation — must see it immediately.
        assert table_exists(wh, "sales_daily") is True
    finally:
        wh.close()


def test_resolved_column_select_as_date_typed_passes_through() -> None:
    rc = ResolvedColumn(name="sale_date", duckdb_type="DATE")
    assert rc.is_date_typed
    expr = rc.select_as_date()
    # Already a DATE — no cast needed.
    assert "CAST" not in expr
    assert "sale_date" in expr


def test_resolved_column_select_as_date_varchar_wraps_in_cast() -> None:
    rc = ResolvedColumn(name="fiscal_week_begin_d", duckdb_type="VARCHAR")
    assert not rc.is_date_typed
    expr = rc.select_as_date()
    assert "CAST" in expr.upper()
    assert "AS DATE" in expr.upper()


def test_dataset_kinds_split_makes_sense() -> None:
    """All 15 datasets must be classified as transactional or dimensional."""
    transactional = {k for k, v in DATASET_KINDS.items() if v == "transactional"}
    dimensional = {k for k, v in DATASET_KINDS.items() if v == "dimensional"}
    # No overlap.
    assert transactional.isdisjoint(dimensional)
    # The well-known dimension tables should be dimensional.
    assert "location_attr" in dimensional
    assert "item_attr" in dimensional
    # The transactional sales/inventory/orders/forecast should be transactional.
    for ds in ("sales_daily", "sales_weekly", "inventory_daily", "forecast_weekly",
               "orders_daily", "po_plan_daily"):
        assert DATASET_KINDS[ds] == "transactional"


def test_column_roles_covers_every_dataset_in_catalog() -> None:
    """Every dataset in the filename catalog should have at least a 'date' role
    declared (or be deliberately empty), so resolve_column doesn't fall through."""
    from bpd_mcp.parsers import PATTERNS

    for p in PATTERNS:
        assert p.dataset in COLUMN_ROLES, (
            f"dataset {p.dataset!r} from PATTERNS has no entry in COLUMN_ROLES; "
            "add one (even if empty) to avoid silent drift."
        )
