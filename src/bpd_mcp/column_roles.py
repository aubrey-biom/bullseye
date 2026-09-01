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


# Snapshot stamp vs content horizon per dataset (Patch #12). For forward-
# looking datasets, detect_date_column's single answer is the SNAPSHOT stamp
# (data freshness) — which HIDES how far into the future the content reaches
# (the §6 trap: forecast_weekly "ends" 2026-07-27 by last_update_d while its
# fiscal weeks run to 2026-10-18). Values are ROLE names resolved through
# COLUMN_ROLES at query time. Datasets absent here have snapshot == content.
DATE_RANGE_ROLES: dict[str, dict[str, str]] = {
    "forecast_weekly": {"snapshot": "snapshot_date", "content": "date"},
    "po_plan_daily": {"snapshot": "date", "content": "order_date"},
    "po_plan_biweekly": {"snapshot": "date", "content": "order_date"},
    "orders_daily": {"snapshot": "snapshot_date", "content": "eta"},
}


# How each dataset's feed behaves at load time (Patch #12) — the standing
# answer to "is this a delta or a snapshot?", which three separate incidents
# had to re-derive from first principles:
#   delta_latest_state      — per-key replace, no date in the key: the table
#                             IS the latest state (query it whole; snapshot
#                             filters return a partial book)
#   accumulating_snapshots  — full snapshot per business_d coexists forever:
#                             ALWAYS filter to one business_d
#   period_replace          — a file is the complete extract of its period(s)
#   append_daily            — one day per file, days accumulate
#   keyed_overwrite_mixed   — newer drops overwrite overlapping keys
#                             (forecast_weekly: neither snapshots nor clean
#                             latest-state; see forecast_drops)
#   dimensional             — full-universe last-write-wins snapshot
FEED_KINDS: dict[str, str] = {
    "sales_daily": "append_daily",
    "sales_weekly": "period_replace",
    "sales_weekly_item": "period_replace",
    "inventory_daily": "append_daily",
    "inventory_weekly": "period_replace",
    "inventory_weekly_item": "period_replace",
    "gross_margin": "period_replace",
    "gross_margin_item": "period_replace",
    "orders_daily": "delta_latest_state",
    "po_plan_daily": "accumulating_snapshots",
    "po_plan_biweekly": "accumulating_snapshots",
    "forecast_weekly": "keyed_overwrite_mixed",
    "item_attr": "dimensional",
    "item_attr_extended": "dimensional",
    "location_attr": "dimensional",
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
    # Patch #12: DATE_RANGE_ROLES entries are validated too — a snapshot or
    # content role drifting from real Target names would silently degrade the
    # dataset listings back to single-date reporting. Those are SOFT
    # (required=False): their only consumers degrade gracefully, so the health
    # check warns instead of failing (review fix: a hard FAIL claimed
    # "analytics tools WILL fail" when none would).
    demanded: dict[str, dict[str, bool]] = {
        ds: dict.fromkeys(roles, True) for ds, roles in REQUIRED_ROLES.items()
    }
    for ds, roles_map in DATE_RANGE_ROLES.items():
        bucket = demanded.setdefault(ds, {})
        for role in roles_map.values():
            bucket.setdefault(role, False)

    failures: list[dict[str, Any]] = []
    for dataset, roles in demanded.items():
        if not table_exists(warehouse, dataset):
            continue
        # Emptiness probe via `__TABLES__` row counts (0 bytes, cached), NOT
        # `SELECT COUNT(*) FROM <logical>`. Under CTE injection each such count
        # runs the full registry body — ~333 MB across the roster, paid at every
        # boot and every health check. See BigQueryWarehouse.base_row_counts.
        if not _dataset_has_rows(warehouse, dataset):
            continue
        for role in sorted(roles):
            try:
                resolve_column(warehouse, dataset, role)
            except ColumnNotFound as e:
                failures.append({**e.detail, "required": roles[role]})
    return failures


@dataclass(frozen=True)
class ResolvedColumn:
    name: str
    """The column name as it exists in the warehouse."""
    sql_type: str
    """Upper-case BigQuery type (e.g. 'DATE', 'TIMESTAMP', 'STRING', 'INT64')."""

    @property
    def is_date_typed(self) -> bool:
        t = self.sql_type.upper()
        # DATE and DATETIME both match the DATE prefix.
        return t.startswith("DATE") or t.startswith("TIMESTAMP")

    def select_as_date(self, *, alias: str | None = None) -> str:
        """SQL expression that returns this column as a DATE.

        If the column is already a DATE/TIMESTAMP the cast is a no-op. Otherwise
        it is a STRING — several bpd_raw feeds ship dates that way, and Target
        pads absent values with a placeholder rather than NULL. SAFE_CAST, not
        CAST, is mandatory here: a plain CAST over one placeholder row aborts the
        whole query with `400 Invalid date: '""'`, so a single bad row in
        location_attr would take down every date-ranged tool. SAFE_CAST yields
        NULL for those rows and the aggregates skip them.
        """
        from .bq import quote_ident

        ident = quote_ident(self.name)
        expr = ident if self.is_date_typed else f"SAFE_CAST({ident} AS DATE)"
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

    Reads the warehouse's cached logical schema (0 bytes, no BigQuery round
    trip). Resolution still happens at CALL time rather than import time, so a
    test that swaps a fixture body into the registry resolves against that body.

    `extra_candidates` lets a caller bolt on dataset-agnostic additional hints
    (e.g. when looking for a date column across multiple datasets).

    Raises `ColumnNotFound` (with rich diagnostic detail) if nothing matches.
    """
    candidates = list(COLUMN_ROLES.get(dataset, {}).get(role, []))
    for c in extra_candidates:
        if c not in candidates:
            candidates.append(c)

    # Call-time introspection against the registry's declared projection.
    cols = _columns_of(warehouse, dataset)

    by_name = {name.lower(): (name, dtype) for name, dtype in cols}
    actual = [name for name, _ in cols]
    for cand in candidates:
        if cand.lower() in by_name:
            real_name, dtype = by_name[cand.lower()]
            return ResolvedColumn(name=real_name, sql_type=str(dtype).upper())

    raise ColumnNotFound(
        detail={
            "dataset": dataset,
            "role": role,
            "candidates": candidates,
            "actual_columns": actual,
        }
    )


def _columns_of(warehouse, table: str) -> list[tuple[str, str]]:
    """`[(column_name, BIGQUERY_TYPE)]` in projection order for a logical table.

    Delegates to `BigQueryWarehouse.logical_schema`, which is cached and costs
    0 bytes. There is deliberately no `information_schema` query here: BigQuery
    scopes INFORMATION_SCHEMA per dataset, so the DuckDB-era
    `FROM information_schema.columns WHERE table_schema='main'` resolved to a
    dataset named `information_schema` and raised 404 NotFound for every role
    lookup — which meant every analytics tool.

    An unknown table yields `[]` so `resolve_column` reports the richer
    ColumnNotFound diagnostic instead of a KeyError.
    """
    try:
        return list(warehouse.logical_schema(table))
    except KeyError:
        return []


def _dataset_has_rows(warehouse, dataset: str) -> bool:
    """Is the logical table's primary base table non-empty? 0 bytes, cached."""
    try:
        entry = warehouse.registry[dataset]
    except (KeyError, TypeError):
        return False
    counts = warehouse.base_row_counts()
    return counts.get(entry.primary_base_table, 0) > 0


def table_exists(warehouse, table: str) -> bool:
    """Is `table` a known logical table? A registry membership test, 0 bytes.

    The registry IS the catalogue now — every logical table is defined there and
    is always queryable, so unlike the DuckDB era (where sync created tables
    lazily and absence was normal) this cannot vary at runtime.
    """
    try:
        return table in warehouse.registry
    except TypeError:
        return False
