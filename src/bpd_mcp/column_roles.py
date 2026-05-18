"""Column-role registry — semantic role → candidate column names per dataset.

Target uses inconsistent column names across BPD datasets (`sale_quantity` vs
`units_sold`, `sales_date` vs `sale_date`, `selected_forecast_q` vs `forecast_units`,
`fiscal_week_begin_d` vs `week_start_date`). Rather than guessing in each analytics
tool, this registry centralizes the mapping. Each tool calls `resolve_column(...)`
at execution time (NOT at module load) so a sync that creates a new table is
visible without restarting the MCP.

Adding a new dataset or a new variant just means appending to a list here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Ordered candidate lists per (dataset, role). First match in the dataset wins.
# Names below reflect what Target *actually* ships (observed in real BPD files
# during validation), not what the original spec guessed.
COLUMN_ROLES: dict[str, dict[str, list[str]]] = {
    # ---------- sales ----------
    "sales_daily": {
        "date": ["sales_date", "sale_date", "transaction_date", "date"],
        "units": ["sale_quantity", "units_sold", "units", "qty", "sales_units"],
        "dollars": [
            "sale_amount",
            "sales_dollars",
            "sales_amt",
            "dollars",
            "revenue",
            "net_sales",
            "gross_sales_amt",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr", "loc_id"],
    },
    "sales_weekly": {
        "date": [
            "sales_date",
            "week_end_date",
            "fiscal_week_end_d",
            "fiscal_week_end_date",
            "sale_date",
        ],
        "units": ["sale_quantity", "units_sold", "units", "qty", "sales_units"],
        "dollars": [
            "sale_amount",
            "sales_dollars",
            "sales_amt",
            "dollars",
            "revenue",
            "net_sales",
            "gross_sales_amt",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr", "loc_id"],
    },
    "sales_weekly_item": {
        "date": [
            "sales_date",
            "week_end_date",
            "fiscal_week_end_d",
            "fiscal_week_end_date",
            "sale_date",
        ],
        "units": ["sale_quantity", "units_sold", "units", "qty", "sales_units"],
        "dollars": ["sale_amount", "sales_dollars", "sales_amt", "dollars"],
        "tcin": ["tcin", "item_id"],
    },
    # ---------- inventory ----------
    "inventory_daily": {
        "date": [
            "business_d",
            "report_date_dim",
            "inventory_date",
            "snapshot_date",
            "inv_date",
            "as_of_date",
        ],
        "on_hand": [
            "on_hand_units",
            "on_hand_qty",
            "inventory_quantity",
            "inv_units",
            "on_hand",
            "stock_units",
            "qty_on_hand",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr", "loc_id"],
    },
    "inventory_weekly": {
        "date": [
            "report_date_dim",
            "week_end_date",
            "fiscal_week_end_d",
            "inventory_date",
            "snapshot_date",
        ],
        "on_hand": [
            "on_hand_units",
            "on_hand_qty",
            "inventory_quantity",
            "inv_units",
            "on_hand",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr", "loc_id"],
    },
    "inventory_weekly_item": {
        "date": [
            "report_date_dim",
            "week_end_date",
            "fiscal_week_end_d",
            "inventory_date",
        ],
        "on_hand": [
            "on_hand_units",
            "on_hand_qty",
            "inventory_quantity",
            "inv_units",
        ],
        "tcin": ["tcin", "item_id"],
    },
    # ---------- gross margin ----------
    "gross_margin": {
        "date": [
            "week_end_date",
            "fiscal_week_end_d",
            "fiscal_week_end_date",
            "report_date_dim",
        ],
        "margin": [
            "gross_margin",
            "gm",
            "gross_margin_pct",
            "margin_pct",
            "margin_amount",
            "gross_margin_amt",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr"],
    },
    "gross_margin_item": {
        "date": [
            "week_end_date",
            "fiscal_week_end_d",
            "fiscal_week_end_date",
            "report_date_dim",
        ],
        "margin": ["gross_margin", "gm", "gross_margin_pct", "margin_pct"],
        "tcin": ["tcin", "item_id"],
    },
    # ---------- item / location attrs ----------
    "item_attr": {
        # Real-data observation: Target ships `processed_ct_date` (and similar
        # `processed_ct_d`) as the "as-of" date on item dimension rows.
        "date": ["processed_ct_date", "processed_ct_d", "as_of_date", "snapshot_date"],
        "tcin": ["tcin", "item_id"],
    },
    "item_attr_extended": {
        "date": [
            "processed_ct_date",
            "processed_ct_d",
            "as_of_date",
            "fiscal_week_end_d",
            "snapshot_date",
        ],
        "tcin": ["tcin", "item_id"],
    },
    "location_attr": {
        # location_attr has multiple date-like columns (last_remodel_date,
        # opening_date, etc.). last_remodel_date is the canonical "latest activity"
        # in real data but it goes back to 2000 — see cache_status dataset_kind.
        "date": [
            "last_remodel_date",
            "opening_date",
            "effective_date",
            "report_date_dim",
            "as_of_date",
        ],
        "location": ["location_id", "location_number", "store_id", "store_nbr", "loc_id"],
    },
    # ---------- orders / PO plan / forecast ----------
    "orders_daily": {
        "date": [
            "order_date",
            "po_date",
            "expected_delivery_date",
            "ship_date",
            "report_date_dim",
        ],
        "units": [
            "open_units",
            "units_open",
            "units_remaining",
            "remaining_units",
            "qty_open",
            "open_qty",
            "qty_remaining",
            "remaining_qty",
            "outstanding_units",
            "outstanding_qty",
            "planned_units",
            "planned_qty",
            "expected_units",
            "expected_qty",
            "po_units",
            "po_qty",
            "order_units",
            "order_qty",
            "ordered_units",
            "ordered_qty",
            "order_quantity",
            "units",
            "qty",
        ],
        "status": [
            "order_status",
            "status",
            "fulfillment_status",
            "ship_status",
            "po_status",
        ],
        "tcin": ["tcin", "item_id"],
        # `receiving_location_id` is orders-specific (destination location for
        # fulfillment) — distinct from the sales/inventory `location_id`. Per
        # Patch #6.2.2, real Target orders files ship this column.
        "location": [
            "receiving_location_id", "location_id", "location_number",
            "store_id", "store_nbr",
        ],
    },
    "po_plan_daily": {
        "date": [
            "plan_date",
            "expected_date",
            "po_date",
            "fiscal_week_begin_d",
            "report_date_dim",
        ],
        "units": [
            "planned_units",
            "planned_qty",
            "planned_quantity",
            "expected_units",
            "po_units",
            "po_qty",
            "units",
            "qty",
        ],
        "tcin": ["tcin", "item_id"],
    },
    "po_plan_biweekly": {
        "date": [
            "period_start_date",
            "period_end_date",
            "fiscal_week_begin_d",
            "plan_date",
            "report_date_dim",
        ],
        "units": [
            "planned_units",
            "planned_qty",
            "planned_quantity",
            "expected_units",
            "po_units",
            "po_qty",
            "units",
            "qty",
        ],
        "tcin": ["tcin", "item_id"],
        "dc": ["dc_id", "dc_number", "dc_nbr"],
    },
    "forecast_weekly": {
        "date": [
            "fiscal_week_begin_d",
            "fiscal_week_begin_date",
            "fiscal_week_end_d",
            "fiscal_week_end_date",
            "forecast_week",
            "week_start_date",
            "week_end_date",
        ],
        "units": [
            "selected_forecast_q",
            "forecast_quantity",
            "forecast_units",
            "forecast_q",
            "fcst_qty",
            "fcst_units",
            "units",
            "qty",
        ],
        "snapshot_date": [
            "last_update_d",
            "snapshot_date",
            "as_of_date",
            "forecast_run_date",
            "snapshot_d",
        ],
        "tcin": ["tcin", "item_id"],
        "location": ["location_id", "location_number", "store_id", "store_nbr"],
    },
}


# Transactional vs dimensional split for the "business data" date range in
# bpd_cache_status (Issue 2 follow-up). Dimensional tables have date columns
# whose extent (e.g. location_attr.last_remodel_date back to year 2000) isn't
# meaningful for "what range of business data do we have".
DATASET_KINDS: dict[str, str] = {
    "sales_daily": "transactional",
    "sales_weekly": "transactional",
    "sales_weekly_item": "transactional",
    "inventory_daily": "transactional",
    "inventory_weekly": "transactional",
    "inventory_weekly_item": "transactional",
    "gross_margin": "transactional",
    "gross_margin_item": "transactional",
    "orders_daily": "transactional",
    "po_plan_daily": "transactional",
    "po_plan_biweekly": "transactional",
    "forecast_weekly": "transactional",
    "item_attr": "dimensional",
    "item_attr_extended": "dimensional",
    "location_attr": "dimensional",
}


@dataclass(frozen=True)
class ResolvedColumn:
    name: str
    """The column name as it exists in the warehouse."""
    duckdb_type: str
    """Upper-case DuckDB type (e.g. 'DATE', 'TIMESTAMP', 'VARCHAR', 'BIGINT')."""

    @property
    def is_date_typed(self) -> bool:
        t = self.duckdb_type.upper()
        return t.startswith("DATE") or t.startswith("TIMESTAMP")

    def select_as_date(self, *, alias: str | None = None) -> str:
        """SQL expression that returns this column as a DATE.

        If the column is already a DATE/TIMESTAMP, the cast is a no-op. If it's
        a VARCHAR (as Target sometimes ships fiscal_week_begin_d), the cast
        applies at query time. Quoted identifier ensures safety.
        """
        from .warehouse import quote_ident

        ident = quote_ident(self.name)
        expr = ident if self.is_date_typed else f"CAST({ident} AS DATE)"
        return f"{expr} AS {quote_ident(alias)}" if alias else expr


class ColumnNotFound(LookupError):
    """Raised when no candidate column for `(dataset, role)` exists in the table.

    Carries enough diagnostic detail (`detail` dict) that callers can surface
    the dataset, role, candidates tried, and actual columns present — so the
    user immediately sees "I need to add column X to the candidate list."
    """

    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(
            f"no candidate column for role={detail['role']!r} in dataset "
            f"{detail['dataset']!r}; tried {detail['candidates']}; "
            f"actual columns: {detail['actual_columns']}"
        )
        self.detail = detail


def resolve_column(
    warehouse,  # avoid circular import on Warehouse type
    dataset: str,
    role: str,
    *,
    extra_candidates: tuple[str, ...] = (),
) -> ResolvedColumn:
    """Find the first candidate column for `(dataset, role)` that actually exists.

    Always queries `information_schema.columns` fresh — no caching. This is the
    Issue-6 fix: tools must see schema changes from a sync without an MCP restart.

    `extra_candidates` lets a caller bolt on dataset-agnostic additional hints
    (e.g. when looking for a date column across multiple datasets).

    Raises `ColumnNotFound` (with rich diagnostic detail) if nothing matches.
    """
    candidates = list(COLUMN_ROLES.get(dataset, {}).get(role, []))
    for c in extra_candidates:
        if c not in candidates:
            candidates.append(c)

    # Fresh introspection — engine sees post-sync schema immediately.
    cols = _columns_of(warehouse, dataset)

    by_name = {name.lower(): (name, dtype) for name, dtype in cols}
    actual = [name for name, _ in cols]
    for cand in candidates:
        if cand.lower() in by_name:
            real_name, dtype = by_name[cand.lower()]
            return ResolvedColumn(name=real_name, duckdb_type=str(dtype).upper())

    raise ColumnNotFound(
        detail={
            "dataset": dataset,
            "role": role,
            "candidates": candidates,
            "actual_columns": actual,
        }
    )


def _columns_of(warehouse, table: str) -> list[tuple[str, str]]:
    """List (column_name, data_type) for a table. Always fresh from info schema."""
    sql = (
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='main' AND table_name='{_safe(table)}' "
        "ORDER BY ordinal_position"
    )
    _, rows = warehouse.execute_sql(sql)
    return [(r[0], r[1]) for r in rows]


def _safe(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum() or ch == "_")


def table_exists(warehouse, table: str) -> bool:
    """Fresh check — does `table` exist in main schema? Re-queried per call."""
    _, rows = warehouse.execute_sql(
        f"SELECT 1 FROM information_schema.tables "
        f"WHERE table_schema='main' AND table_name='{_safe(table)}'"
    )
    return bool(rows)
