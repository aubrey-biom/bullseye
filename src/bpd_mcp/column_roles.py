"""Column-role registry — semantic role → candidate column names per dataset.

Target uses inconsistent column names across BPD datasets (`sale_quantity` vs
`units_sold`, `sales_date` vs `sale_date`, `selected_forecast_q` vs `forecast_units`,
`fiscal_week_begin_d` vs `week_start_date`). Rather than guessing in each analytics
tool, this registry centralizes the mapping. Each tool calls `resolve_column(...)`
at execution time (NOT at module load) so a sync that creates a new table is
visible without restarting the MCP.

Adding a new dataset or a new variant just means appending to a list here.
Real Target names go FIRST in each candidate list; older/invented aliases stay
behind them for fixture compatibility and rename tolerance.

Target BPD column-naming convention (Patch #10 — documentation only; nothing
may programmatically suffix-match, resolution is always exact against these
ordered lists):

    suffix          meaning                          example
    ------          -------                          -------
    _q              quantity (units)                 ending_on_hand_q, ordered_q
    _a              amount (USD)                     net_sales_a, sale_amount*
    _d              date                             business_d, order_d, snapshot_d
    _f              flag (boolean, often as "")      purchase_order_active_f
    _c              code                             (various)
    _id             identifier                       purchase_order_id, location_id
    _percentage     percent on a 0-100 scale         instock_percentage

    * sales feeds predate the convention and ship sale_amount/sale_quantity.

Bookend prefixes `beginning_` / `ending_` appear on inventory measures; the
period-end (`ending_`) value is the default meaning of "on hand" and is always
ordered first. The location key is `location_id` in sales/inventory/forecast
feeds but `receiving_location_id` (the destination DC/store) in orders_daily
and po_plan_*. See also parsers.CANONICAL_RENAMES for cross-generation renames.
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
        # Patch #10 (P0-1): real Target columns are `ending_on_hand_q` /
        # `beginning_on_hand_q`, exactly as in inventory_weekly below — the
        # Patch #7.1 correction was applied to the weekly siblings but missed
        # this dataset, hard-failing get_inventory_snapshot/get_sell_through
        # whenever inventory_daily was loaded. Invented aliases kept behind.
        "on_hand": [
            "ending_on_hand_q",
            "beginning_on_hand_q",
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
        # Patch #7.1: real Target column is `business_d` (sibling fix to the
        # inventory_daily change in #6.2.2).
        "date": [
            "business_d",
            "report_date_dim",
            "week_end_date",
            "fiscal_week_end_d",
            "inventory_date",
            "snapshot_date",
        ],
        # Real Target on-hand columns are `beginning_on_hand_q` and
        # `ending_on_hand_q` (the `_q` suffix is "quantity", `_a` is "amount/$").
        # Older aliases kept for fixture compatibility.
        "on_hand": [
            "ending_on_hand_q",
            "beginning_on_hand_q",
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
        # Patch #7.1: same `business_d` fix as the locational sibling.
        "date": [
            "business_d",
            "report_date_dim",
            "week_end_date",
            "fiscal_week_end_d",
            "inventory_date",
        ],
        "on_hand": [
            "ending_on_hand_q",
            "beginning_on_hand_q",
            "on_hand_units",
            "on_hand_qty",
            "inventory_quantity",
            "inv_units",
        ],
        "tcin": ["tcin", "item_id"],
    },
    # ---------- gross margin ----------
    "gross_margin": {
        # `fiscal_week_end_d` is the real Target column (Patch #7). `week_end_date`
        # remains as a legacy alias.
        "date": [
            "fiscal_week_end_d",
            "week_end_date",
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
        # Origination-side location is distinct from `location` (the fulfillment
        # location). For in-store purchases they match; for online orders fulfilled
        # by a different store, they differ. Patch #7 — part of the natural PK.
        "location_originated": ["location_id_originated"],
        # Channel + fulfillment dimensions also part of the natural PK.
        "channel_originated": ["channel_originated"],
        "channel_fulfilled": ["channel_fulfilled"],
        "fulfillment_type": ["fulfillment_type"],
        "fulfillment_subtype": ["fulfillment_subtype"],
    },
    "gross_margin_item": {
        "date": [
            "fiscal_week_end_d",
            "week_end_date",
            "fiscal_week_end_date",
            "report_date_dim",
        ],
        "margin": ["gross_margin", "gm", "gross_margin_pct", "margin_pct"],
        "tcin": ["tcin", "item_id"],
        # Same channel/fulfillment dimensions as gross_margin (Patch #7).
        "channel_originated": ["channel_originated"],
        "channel_fulfilled": ["channel_fulfilled"],
        "fulfillment_type": ["fulfillment_type"],
        "fulfillment_subtype": ["fulfillment_subtype"],
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
        # Patch #10 (P0-1): the old "units"/"status" roles were 20+ invented
        # names (open_units, order_status, ...) — no such columns exist in real
        # Target orders files, so get_open_orders hard-failed. There is NO
        # physical "open units" column: open units are DERIVED as
        # ordered - received - cancel_remaining (see get_open_orders). The
        # roles below name the real derivation inputs.
        "ordered": ["revised_order_q", "original_order_q"],
        "received": ["item_received_q"],
        "cancel_remaining": ["cancel_remaining_order_q"],
        "po_id": ["purchase_order_id", "purchase_order_number"],
        # snapshot_d is the file's as-of stamp; the table itself is latest-state
        # per (purchase_order_id, tcin, receiving_location_id) — see FEED notes.
        "snapshot_date": ["snapshot_d"],
        "order_created": ["purchase_order_create_d"],
        "eta": ["revised_estimated_arrival_d", "original_estimated_arrival_d"],
        # Generic "date" kept for detect_date_column's registry tier; snapshot
        # stamp first, then legacy invented aliases.
        "date": [
            "snapshot_d",
            "purchase_order_create_d",
            "order_date",
            "po_date",
            "report_date_dim",
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
        # `business_d` is the as-of date Target ships (Patch #7). The other
        # entries are legacy aliases for fixture/historical compatibility.
        "date": [
            "business_d",
            "plan_date",
            "expected_date",
            "po_date",
            "fiscal_week_begin_d",
            "report_date_dim",
        ],
        # `order_d` is the day each planned PO is targeted at — distinct from
        # `business_d` (the as-of snapshot). Both are part of the natural PK.
        "order_date": ["order_d", "order_date"],
        # Same orders-specific destination-location concept as orders_daily.
        "receiving_location": ["receiving_location_id"],
        # Patch #10 (P0-1): `ordered_q` is the real Target column — it was
        # already first in po_plan_biweekly's list but missing here.
        "units": [
            "ordered_q",
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
        # Patch #7.1: real Target shape is the same as po_plan_daily —
        # `business_d` is the as-of date, `order_d` is per-row, and the
        # natural-key location dimension is `receiving_location_id` (not
        # `dc_id`). The legacy DC/period roles are kept for older fixtures.
        "date": [
            "business_d",
            "period_start_date",
            "period_end_date",
            "fiscal_week_begin_d",
            "plan_date",
            "report_date_dim",
        ],
        "order_date": ["order_d", "order_date"],
        "receiving_location": ["receiving_location_id"],
        "units": [
            "ordered_q",
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


# Roles a dataset MUST resolve for the analytics tools that query it to work
# (Patch #10). Derived from which resolve_column calls in tools/query.py are
# NOT wrapped in try/except. Datasets absent here have no tool that hard-
# depends on them. Consumed by `validate_roles` → the `roles_resolvable`
# health check, which makes "the candidate lists match real Target names"
# an executable claim instead of a hope.
REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    "sales_daily": ("date", "units", "tcin", "location"),
    "sales_weekly": ("date", "units", "tcin", "location"),
    "inventory_daily": ("date", "on_hand", "tcin", "location"),
    "inventory_weekly": ("date", "on_hand", "tcin", "location"),
    "gross_margin": ("date", "tcin"),
    "orders_daily": (
        "ordered", "received", "cancel_remaining", "po_id", "tcin", "location",
    ),
    "po_plan_daily": ("date", "order_date", "units", "tcin"),
    "po_plan_biweekly": ("date", "order_date", "units", "tcin"),
    "forecast_weekly": ("date", "units", "tcin"),
}


# Columns Target ships but never populates (every row is their `""` NULL
# placeholder — see parsers._attempt_strict and Patch #6). This is a
# data-source fact, not a parser bug: the boolean caster demonstrably handles
# mixed values (tests/test_parsers.py). No tool may filter on these columns;
# a drift-guard test enforces that none appears in any COLUMN_ROLES candidate
# list, and the `known_unpopulated_columns` health check warns if Target ever
# starts populating one (at which point it can be promoted to a real role).
KNOWN_UNPOPULATED_AT_SOURCE: dict[str, tuple[str, ...]] = {
    "orders_daily": ("purchase_order_active_f",),
}


def validate_roles(warehouse) -> list[dict[str, Any]]:
    """Check every REQUIRED_ROLES entry against the live schema.

    Only POPULATED tables are validated: tables are created lazily by sync, so
    absent/empty tables are expected on a fresh install and are skipped —
    "fail at boot" is the wrong shape for this architecture (see module
    docstring on call-time resolution). Returns one failure dict per
    unresolvable (dataset, role), each carrying the ColumnNotFound diagnostic
    detail (candidates tried, actual columns present).
    """
    failures: list[dict[str, Any]] = []
    for dataset, roles in REQUIRED_ROLES.items():
        if not table_exists(warehouse, dataset):
            continue
        _, rows = warehouse.execute_sql(
            f"SELECT COUNT(*) FROM {_safe(dataset)}"
        )
        if not rows or rows[0][0] == 0:
            continue
        for role in roles:
            try:
                resolve_column(warehouse, dataset, role)
            except ColumnNotFound as e:
                failures.append(e.detail)
    return failures


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
