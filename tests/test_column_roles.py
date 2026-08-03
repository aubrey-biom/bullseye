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


# --------- Patch #10: REQUIRED_ROLES against real Target headers ---------

# The real (post-parse, lowercased) column sets Target ships per dataset, as
# observed during live validation. This makes "the candidate lists match real
# Target names" an executable claim: if Target renames a column AND the
# candidate list lacks the new name, this test (and the roles_resolvable
# health check) fails instead of one analytics tool at a time.
REAL_TARGET_HEADERS: dict[str, list[str]] = {
    "sales_daily": [
        "sales_date", "vendor_id", "barcode", "tcin", "dpci",
        "origination_channel", "reporting_channel", "fulfillment_type",
        "location_id", "sale_amount", "sale_quantity",
    ],
    "sales_weekly": [
        "sales_date", "vendor_id", "barcode", "tcin", "dpci",
        "origination_channel", "reporting_channel", "fulfillment_type",
        "location_id", "sale_amount", "sale_quantity",
    ],
    "inventory_daily": [
        "business_d", "primary_vendor_id", "tcin", "dpci", "location_id",
        "beginning_on_hand_q", "ending_on_hand_q", "ending_on_transfer_q",
    ],
    "inventory_weekly": [
        "business_d", "primary_vendor_id", "tcin", "dpci", "location_id",
        "beginning_on_hand_q", "ending_on_hand_q", "ending_on_transfer_q",
    ],
    "gross_margin": [
        "fiscal_week_end_d", "vendor_id", "tcin", "dpci",
        "channel_originated", "location_id_originated", "location_id",
        "channel_fulfilled", "fulfillment_type", "fulfillment_subtype",
        "net_sales_a", "net_sales_q", "adjusted_gross_margin_a",
    ],
    "orders_daily": [
        "snapshot_d", "purchase_order_id", "purchase_order_create_d",
        "tcin", "dpci", "receiving_location_id",
        "original_order_q", "revised_order_q", "item_received_q",
        "cancel_remaining_order_q", "original_estimated_arrival_d",
        "revised_estimated_arrival_d", "purchase_order_active_f",
    ],
    "po_plan_daily": [
        "business_d", "tcin", "dpci", "order_d",
        "receiving_location_id", "ordered_q",
    ],
    "po_plan_biweekly": [
        "business_d", "tcin", "dpci", "order_d",
        "receiving_location_id", "ordered_q",
    ],
    "forecast_weekly": [
        "fiscal_week_begin_d", "last_update_d", "tcin", "location_id",
        "selected_forecast_q",
    ],
}


def test_required_roles_resolve_against_real_target_headers(tmp_path: Path) -> None:
    from bpd_mcp.column_roles import REQUIRED_ROLES, validate_roles

    assert set(REAL_TARGET_HEADERS) == set(REQUIRED_ROLES), (
        "keep REAL_TARGET_HEADERS in lockstep with REQUIRED_ROLES"
    )
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        for ds, cols in REAL_TARGET_HEADERS.items():
            ddl_cols = ", ".join(f"{c} VARCHAR" for c in cols)
            wh.execute_sql(f"CREATE TABLE {ds} ({ddl_cols})")
            placeholders = ", ".join(["NULL"] * len(cols))
            wh.execute_sql(f"INSERT INTO {ds} VALUES ({placeholders})")
        failures = validate_roles(wh)
        assert failures == [], (
            "required role(s) unresolvable against real Target headers: "
            f"{failures}"
        )
        # Spot-check the P0-1 fixes resolve to the REAL columns, not aliases.
        assert resolve_column(wh, "inventory_daily", "on_hand").name == "ending_on_hand_q"
        assert resolve_column(wh, "orders_daily", "ordered").name == "revised_order_q"
        assert resolve_column(wh, "orders_daily", "location").name == "receiving_location_id"
        assert resolve_column(wh, "po_plan_daily", "units").name == "ordered_q"
        assert resolve_column(wh, "po_plan_daily", "order_date").name == "order_d"
    finally:
        wh.close()


def test_validate_roles_skips_empty_and_absent_tables(tmp_path: Path) -> None:
    from bpd_mcp.column_roles import validate_roles

    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # Absent tables: nothing to validate.
        assert validate_roles(wh) == []
        # Present but EMPTY table with hopeless columns: still skipped.
        wh.execute_sql("CREATE TABLE inventory_daily (nothing_useful VARCHAR)")
        assert validate_roles(wh) == []
        # One row makes it validate — and fail.
        wh.execute_sql("INSERT INTO inventory_daily VALUES ('x')")
        failures = validate_roles(wh)
        assert {(f["dataset"], f["role"]) for f in failures} >= {
            ("inventory_daily", "on_hand"),
            ("inventory_daily", "date"),
        }
    finally:
        wh.close()


def test_known_unpopulated_columns_never_in_candidate_lists() -> None:
    """No tool may resolve (and then filter on) a column Target never
    populates. Guards both the central registry and the Patch-#10 requirement
    that query.py's parallel candidate tuples stay deleted."""
    from bpd_mcp import column_roles as cr
    from bpd_mcp.tools import query as query_mod

    banned = {
        col for cols in cr.KNOWN_UNPOPULATED_AT_SOURCE.values() for col in cols
    }
    for ds, roles in cr.COLUMN_ROLES.items():
        for role, candidates in roles.items():
            hits = banned.intersection(candidates)
            assert not hits, f"{ds}.{role} lists known-unpopulated column(s) {hits}"
    # The divergent local resolver must never come back.
    for stale in (
        "_QTY_COL_CANDIDATES",
        "_DATE_COL_CANDIDATES",
        "_LOC_COL_CANDIDATES",
        "_STATUS_COL_CANDIDATES",
        "_first_present",
    ):
        assert not hasattr(query_mod, stale), (
            f"tools/query.py regrew {stale} — route through column_roles instead"
        )


def test_required_roles_datasets_exist_in_registry() -> None:
    from bpd_mcp.column_roles import COLUMN_ROLES, REQUIRED_ROLES

    for ds, roles in REQUIRED_ROLES.items():
        assert ds in COLUMN_ROLES, f"REQUIRED_ROLES references unknown dataset {ds}"
        for role in roles:
            assert role in COLUMN_ROLES[ds], (
                f"REQUIRED_ROLES demands {ds}.{role} but the registry has no "
                "candidate list for it"
            )


def test_feed_kinds_and_date_range_roles_complete_and_consistent() -> None:
    """Patch #12 drift guards: FEED_KINDS covers every dataset; DATE_RANGE_ROLES
    references only roles that exist in the registry (validate_roles enforces
    them at runtime, this enforces them at CI time)."""
    from typing import get_args

    from bpd_mcp.column_roles import COLUMN_ROLES, DATE_RANGE_ROLES, FEED_KINDS
    from bpd_mcp.parsers import Dataset

    all_datasets = set(get_args(Dataset))
    assert set(FEED_KINDS) == all_datasets, (
        "every dataset needs a feed_kind (and no strays)"
    )
    allowed = {
        "delta_latest_state", "accumulating_snapshots", "period_replace",
        "append_daily", "keyed_overwrite_mixed", "dimensional",
    }
    assert set(FEED_KINDS.values()) <= allowed
    for ds, roles_map in DATE_RANGE_ROLES.items():
        assert ds in all_datasets
        assert set(roles_map) == {"snapshot", "content"}
        for role in roles_map.values():
            assert role in COLUMN_ROLES[ds], (
                f"DATE_RANGE_ROLES demands {ds}.{role} but the registry has "
                "no candidate list for it"
            )


def test_validate_roles_flags_date_range_roles_as_soft(tmp_path: Path) -> None:
    """Patch #12 pin (mutation-proof): a populated table missing a role that
    only DATE_RANGE_ROLES demands must surface as a required=False failure —
    dropping the DATE_RANGE_ROLES merge from validate_roles breaks this."""
    from bpd_mcp.column_roles import validate_roles

    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        # forecast_weekly WITHOUT any snapshot_date candidate: 'date'/'units'/
        # 'tcin' (REQUIRED_ROLES) resolve, 'snapshot_date' (DATE_RANGE_ROLES
        # only) cannot.
        wh.execute_sql(
            "CREATE TABLE forecast_weekly (tcin BIGINT, location_id BIGINT, "
            "fiscal_week_begin_d VARCHAR, selected_forecast_q BIGINT)"
        )
        wh.execute_sql(
            "INSERT INTO forecast_weekly VALUES (100, 1, '2026-05-03', 5)"
        )
        failures = validate_roles(wh)
        assert [(f["dataset"], f["role"], f["required"]) for f in failures] == [
            ("forecast_weekly", "snapshot_date", False)
        ]
    finally:
        wh.close()
