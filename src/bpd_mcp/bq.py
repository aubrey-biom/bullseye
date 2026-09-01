"""BigQuery data layer: logical-table registry, CTE injection, and the warehouse.

This module replaces the local DuckDB warehouse. The motivation is not
performance — it is *concurrency*. DuckDB permits exactly one process to hold
the database file; Claude Desktop now spawns a second copy of this server for
Cowork/Code sessions, and the second copy could not open the file at all (not
even `read_only=True`) while the first held it. BigQuery is a network service,
so the lock disappears by construction.

Three things live here, in dependency order:

1. **The logical-table registry** (`LOGICAL_TABLES`). The analytics tools in
   `tools/query.py` compose SQL against bare names like `sales_daily` and
   `forecast_weekly`. Under DuckDB those were physical tables written by the
   ingest half. Under BigQuery they are *projections* over
   `biom-reporting-s26.biom_canvas.*` and `.bpd_raw.*`, defined here as SQL
   bodies. The read-only service account cannot `CREATE VIEW`, so the
   projections cannot be materialised server-side — hence (2).

2. **CTE injection** (`build`). Before any statement reaches BigQuery, the
   logical tables it actually references are prepended as a `WITH` block. Only
   referenced tables are injected: blanket injection would add roughly 400 MB
   of avoidable scan to every tool call.

3. **`BigQueryWarehouse`** — the same small read surface the tools already
   call on the old `Warehouse` object (`execute_sql`, `describe`,
   `detect_date_column`, `list_datasets`, `close`, `read_only`, `db_path`),
   plus the BigQuery-specific additions the swap needs (`dry_run`,
   `logical_schema`, `freshness_stats`, `refresh_metadata`).

Cost discipline is a first-class concern here in a way it never was on a local
file. Three facts drive the design and are load-bearing:

  * A `dry_run` of any query — including one whose only source is a real fact
    table — bills **0 bytes** and returns the full typed result schema. All
    schema introspection goes through that path.
  * `SELECT ... FROM <dataset>.__TABLES__` bills **0 bytes**. All row counts go
    through that path. Any `INFORMATION_SCHEMA.*` query bills a 10 MB minimum,
    and — critically — CTE-injected logical tables appear in no catalogue at
    all, so `INFORMATION_SCHEMA` cannot answer questions about them anyway.
  * BigQuery pushes caller predicates *into* injected CTEs, so partition
    pruning survives injection (`sales_daily` 13.3 MB unfiltered → 3.5 MB with
    a date predicate). Never drop a `WHERE` in the name of simplification.

Read-only-ness is now a property of the *credential*, not of a transaction
wrapper: the service account holds `dataViewer` + `jobUser` and gets a 403 on
`bigquery.tables.create` for both `CREATE VIEW` and `CREATE TABLE`. That is
strictly stronger than the `BEGIN TRANSACTION READ ONLY` facade it replaces.
`sql_safety.py` remains as defense-in-depth above it.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.cloud import bigquery

from .logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.cloud.bigquery.job import QueryJob
    from google.cloud.bigquery.table import RowIterator

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

BQ_PROJECT_DEFAULT = "biom-reporting-s26"

# `location` is NOT optional and is threaded into every job. Omitting it makes
# INFORMATION_SCHEMA silently return zero rows — a bug that presents as "the
# table has no columns" rather than as an error.
BQ_LOCATION_DEFAULT = "us-central1"

# Dataset holding the Kiteworks → GCS → BigQuery pipeline's per-file ledger.
# 834 rows today; the only honest source for "how fresh is this feed?".
BQ_META_DATASET = "bpd_meta"
BQ_INGESTION_STATE = "ingestion_state"

# Project the registry bodies are written against. The `project` passed to
# BigQueryWarehouse selects the *billing/job* project; the source references
# inside the CTE bodies below are fully qualified against this constant. They
# are the same value in every deployment we have; if that ever stops being
# true, the registry bodies (not the client) are what must change.
_P = BQ_PROJECT_DEFAULT


# --------------------------------------------------------------------------------------
# The logical-table registry
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogicalTable:
    """One logical table the analytics tools may reference by bare name.

    `sql` is injected verbatim as a CTE body. It MUST be a complete, standalone
    SELECT that runs on its own, and MUST project lowercase column names (see
    `column_contract`).
    """

    name: str

    sql: str
    """CTE body. Fully qualified, backtick-quoted source refs. No trailing semicolon."""

    base_tables: tuple[str, ...]
    """Fully qualified `project.dataset.table` names this body reads, for
    `__TABLES__` row counts, health checks and cost attribution. The FIRST entry
    is the PRIMARY source — the one `describe()` reports `row_count` from."""

    date_column: str
    """The projected column `detect_date_column()` returns for this table.

    DECLARED, not probed. The DuckDB implementation ran one
    `SELECT COUNT(col) FROM t` per candidate column per call (the all-NULL
    guard); free on a local file, a full column scan each on BigQuery, fanned
    out once per dataset from `list_datasets`. Declaring it also handles the
    all-NULL hazard that probe existed for. `heuristic_date_column()` keeps the
    schema-only tiering rules so a drift test can assert the two agree."""

    patterns: tuple[str, ...] = ()
    """`bpd_meta.ingestion_state.pattern` value(s) that feed this table. Used to
    answer "how fresh is sales_daily" and to derive active/retired status.
    Empty = no direct Kiteworks file feed."""

    latest_state_note: str | None = None
    """Non-None when the body applies a QUALIFY/dedup to reproduce a DuckDB
    upsert semantic. Rendered by `describe()` so the reduction is never
    invisible to a caller reading raw row counts."""

    depends_on: tuple[str, ...] = ()
    """Other LOGICAL table names this body references by bare name. Empty for
    every BPD table today; exists so the extension seam below can compose."""

    column_contract: tuple[str, ...] = field(default=())
    """Column names callers may rely on. Documentary today; a drift test
    asserts every listed name appears in the body's dry-run schema."""

    @property
    def primary_base_table(self) -> str:
        """The base table `row_count` is reported from."""
        return self.base_tables[0]


# ---------------------------------------------------------------------------
# EXTENSION SEAM — adding a non-BPD source (Shopify, Loop, ...)
#
# Adding a logical table is a data change, not a code change. Append one
# LogicalTable entry:
#
#   LOGICAL_TABLES["shopify_orders_daily"] = LogicalTable(
#       name="shopify_orders_daily",
#       sql="SELECT order_date, ... FROM `biom-reporting-s26.biom_canvas.fct_shopify_orders` WHERE is_current",
#       base_tables=("biom-reporting-s26.biom_canvas.fct_shopify_orders",),
#       date_column="order_date",
#       patterns=(),            # not a Kiteworks BPD file feed
#   )
#
# Everything downstream picks it up automatically because it all reads THIS dict:
#   - CTE injection            (build)
#   - describe() / bpd://schema
#   - table_exists / _columns_of / resolve_column
#   - detect_date_column
#   - list_datasets, health checks, drift guards
#
# Four things you must ALSO do, none of them in this file:
#   1. COLUMN_ROLES[name]  — role -> candidate columns, or resolve_column
#      raises ColumnNotFound and analytics tools return SCHEMA_INCOMPATIBLE.
#   2. DATASET_KINDS[name] — "transactional" or "dimensional". A drift guard
#      asserts these key sets are identical; a missing entry fails the suite.
#   3. FEED_KINDS[name]    — the load semantic. If the source is an
#      accumulating snapshot, decide ONE owner of the latest-state reduction:
#      the registry body (like orders_daily / forecast_weekly) OR the tool
#      (like po_plan_*). Never both — they fight, silently.
#   4. schemas.KnownDataset — regenerated from LOGICAL_TABLES keys
#      (`KNOWN_DATASET_NAMES` below is the generated tuple).
#
# Composing across sources: put the other logical name in `depends_on` and
# reference it by bare name inside `sql`. CTE injection resolves transitively
# and emits in topological order. No BPD table uses this today.
#
# Cost: an unpartitioned source scans in full on every reference. The three
# biom_canvas facts are partitioned on their date column and clustered on
# (is_current, tcin); ALL 13 bpd_raw tables are unpartitioned and unclustered.
# Prefer a canvas table over a raw landing table, and declare `date_column` so
# nothing has to probe for it.
#
# Numeric footgun worth knowing before you add a measure: every measure a role
# can reach today is INT64 or FLOAT64, so the `* 1.0` / `* 100.0` promotions in
# tools/query.py land on FLOAT64. `dim_product.current_price` is NUMERIC, and
# NUMERIC * 1.0 stays NUMERIC — division then rounds at 9 decimals instead of
# promoting. If a role ever resolves to a NUMERIC column, check the ratio math.
# ---------------------------------------------------------------------------


LOGICAL_TABLES: dict[str, LogicalTable] = {}


def _register(t: LogicalTable) -> None:
    LOGICAL_TABLES[t.name] = t


# ---------- sales ----------

_register(
    LogicalTable(
        name="sales_daily",
        # The canvas fact is the DEDUPED raw feed, not a lossy summary: the raw
        # DAILY_SALES_TCIN_LOC drop carries 492,734 rows over 489,420 distinct
        # natural keys, and canvas `data_grain='daily'` is exactly 489,420. So
        # this is strictly better than reading bpd_raw, and it is partitioned.
        sql=f"""
SELECT sales_date, tcin, location_id, origination_channel, reporting_channel, fulfillment_type,
       vendor_id, barcode, dpci, manufacturer_style, dept, class, item_description,
       original_location_id, original_reporting_channel, original_origination_channel,
       sale_amount, sale_quantity, circular_sale_amount, circular_sale_quantity,
       clearance_sale_amount, clearance_sale_quantity, promo_sale_amount, promo_sale_quantity,
       regular_sale_amount, regular_sale_quantity, circle_sale_amount, circle_sale_quantity,
       mature_sale_amount, mature_sale_quantity, comparable_sale_amount, comparable_sale_quantity,
       ad_comparable_sale_amount, ad_comparable_sale_quantity, return_guest_amount,
       return_guest_quantity, drive_up_sale_a, drive_up_sale_q, shipt_app_sale_a, shipt_app_sale_q,
       shipt_target_sale_a, shipt_target_sale_q
FROM `{_P}.biom_canvas.fct_target_sales`
WHERE is_current AND NOT is_deleted AND data_grain = 'daily'
""",
        base_tables=(f"{_P}.biom_canvas.fct_target_sales",),
        date_column="sales_date",
        patterns=("DAILY_SALES_TCIN_LOC",),
        column_contract=("sales_date", "tcin", "location_id", "sale_quantity", "sale_amount"),
    )
)

_register(
    LogicalTable(
        name="sales_weekly",
        # canvas ∪ raw. The canvas weekly grains stop at 2026-05-02 while the
        # raw WEEKLY_SALES_TCIN_LOC feed runs to 2026-08-29 — the DuckDB
        # warehouse had those 17 weeks because it loaded the Kiteworks file
        # directly, so a canvas-only definition would be a REGRESSION, not a
        # simplification. The union restores parity.
        #
        # The MAX() boundary is dynamic ON PURPOSE: it self-heals the day the
        # canvas pipeline catches up. Do not substitute a static date.
        #
        # Do NOT union `bpd_raw.history_sales_weekly` as a third branch: it
        # overlaps weekly_sales_tcin_loc on 2026-04-04..2026-05-02 and would
        # double-count. (HISTORY_SALES_WEEKLY is still listed in `patterns` —
        # it is a feed that lands in canvas, not a source read here.)
        sql=f"""
SELECT sales_date, tcin, location_id, origination_channel, reporting_channel, fulfillment_type,
       vendor_id, barcode, dpci, manufacturer_style, dept, class, item_description,
       sale_amount, sale_quantity, drive_up_sale_a, drive_up_sale_q
FROM `{_P}.biom_canvas.fct_target_sales`
WHERE is_current AND NOT is_deleted AND data_grain IN ('weekly','history_weekly')
UNION ALL
SELECT SALES_DATE, TCIN, LOCATION_ID, ORIGINATION_CHANNEL, REPORTING_CHANNEL, FULFILLMENT_TYPE,
       VENDOR_ID, BARCODE, DPCI, MANUFACTURER_STYLE, DEPT, CLASS, ITEM_DESCRIPTION,
       SALE_AMOUNT, SALE_QUANTITY, DRIVE_UP_SALE_A, DRIVE_UP_SALE_Q
FROM `{_P}.bpd_raw.weekly_sales_tcin_loc`
WHERE SALES_DATE > (SELECT MAX(sales_date) FROM `{_P}.biom_canvas.fct_target_sales`
                    WHERE is_current AND NOT is_deleted AND data_grain IN ('weekly','history_weekly'))
""",
        # NOTE on row_count: the primary base table is the shared sales fact,
        # so describe()'s row_count covers daily + weekly grains together and
        # OVERSTATES this logical table. `row_count_basis` says so.
        base_tables=(
            f"{_P}.biom_canvas.fct_target_sales",
            f"{_P}.bpd_raw.weekly_sales_tcin_loc",
        ),
        date_column="sales_date",
        patterns=("WEEKLY_SALES_TCIN_LOC", "HISTORY_SALES_WEEKLY"),
        column_contract=("sales_date", "tcin", "location_id", "sale_quantity", "sale_amount"),
    )
)

_register(
    LogicalTable(
        name="sales_weekly_item",
        # Item-grain rollup. Target sunset the *_TCIN family ~2026-05-16, so
        # expect this to read stale. Registered anyway: dropping it would
        # silently remove capability the server has today and would break the
        # DATASET_KINDS / FEED_KINDS / KnownDataset drift guards.
        sql=f"""
SELECT SALES_DATE AS sales_date, TCIN AS tcin, VENDOR_ID AS vendor_id, BARCODE AS barcode,
       DPCI AS dpci, MANUFACTURER_STYLE AS manufacturer_style, DEPT AS dept, CLASS AS class,
       ITEM_DESCRIPTION AS item_description, ORIGINATION_CHANNEL AS origination_channel,
       REPORTING_CHANNEL AS reporting_channel, FULFILLMENT_TYPE AS fulfillment_type,
       SALE_AMOUNT AS sale_amount, SALE_QUANTITY AS sale_quantity
FROM `{_P}.bpd_raw.weekly_sales_tcin`
""",
        base_tables=(f"{_P}.bpd_raw.weekly_sales_tcin",),
        date_column="sales_date",
        patterns=("WEEKLY_SALES_TCIN",),
        column_contract=("sales_date", "tcin", "sale_quantity", "sale_amount"),
    )
)


# ---------- inventory ----------

# `inventory_date AS business_d` — the alias is LOAD-BEARING, not cosmetic.
# `business_d` appears 60x across tests/ and `inventory_date` 0x, and the name
# is echoed verbatim into every response's `extra.date_col`. It is also exactly
# the rename parsers.CANONICAL_RENAMES applied at load time
# (`inventory_weekly: {"week_end_d": "business_d"}`), so keeping it preserves
# the public column contract the DuckDB warehouse published.
_register(
    LogicalTable(
        name="inventory_daily",
        sql=f"""
SELECT inventory_date AS business_d, tcin, location_id, primary_vendor_id, department_id, class_id,
       dpci, manufacturer_style, item_description,
       ending_on_hand_a, ending_on_hand_q, ending_on_transfer_a, ending_on_transfer_q,
       ending_on_purchase_a, ending_on_purchase_q, instock_q, instock_percentage,
       out_of_stock_q, out_of_stock_percentage, tracked_item_out_of_stock_q
FROM `{_P}.biom_canvas.fct_target_inventory`
WHERE is_current AND NOT is_deleted AND data_grain = 'daily'
""",
        base_tables=(f"{_P}.biom_canvas.fct_target_inventory",),
        date_column="business_d",
        patterns=("DAILY_INV_TCIN_LOC",),
        column_contract=("business_d", "tcin", "location_id", "ending_on_hand_q"),
    )
)

_register(
    LogicalTable(
        name="inventory_weekly",
        # There is NO data_grain='weekly' in fct_target_inventory — the only two
        # values that exist are 'daily' and 'history_weekly'. The raw
        # WEEKLY_INV_TCIN_LOC feed carries everything after the canvas horizon,
        # unioned on the same self-healing MAX() boundary as sales_weekly.
        sql=f"""
SELECT inventory_date AS business_d, tcin, location_id, primary_vendor_id, department_id, class_id,
       dpci, manufacturer_style, item_description,
       ending_on_hand_a, ending_on_hand_q, ending_on_transfer_a, ending_on_transfer_q,
       ending_on_purchase_a, ending_on_purchase_q, instock_q, instock_percentage,
       out_of_stock_q, out_of_stock_percentage, tracked_item_out_of_stock_q
FROM `{_P}.biom_canvas.fct_target_inventory`
WHERE is_current AND NOT is_deleted AND data_grain = 'history_weekly'
UNION ALL
SELECT BUSINESS_D, TCIN, LOCATION_ID, PRIMARY_VENDOR_ID, DEPARTMENT_ID, CLASS_ID,
       DPCI, MANUFACTURER_STYLE, ITEM_DESCRIPTION,
       ENDING_ON_HAND_A, ENDING_ON_HAND_Q, ENDING_ON_TRANSFER_A, ENDING_ON_TRANSFER_Q,
       ENDING_ON_PURCHASE_A, ENDING_ON_PURCHASE_Q, INSTOCK_Q, INSTOCK_PERCENTAGE,
       OUT_OF_STOCK_Q, OUT_OF_STOCK_PERCENTAGE, TRACKED_ITEM_OUT_OF_STOCK_Q
FROM `{_P}.bpd_raw.weekly_inv_tcin_loc`
WHERE BUSINESS_D > (SELECT MAX(inventory_date) FROM `{_P}.biom_canvas.fct_target_inventory`
                    WHERE is_current AND NOT is_deleted AND data_grain = 'history_weekly')
""",
        base_tables=(
            f"{_P}.biom_canvas.fct_target_inventory",
            f"{_P}.bpd_raw.weekly_inv_tcin_loc",
        ),
        date_column="business_d",
        patterns=("WEEKLY_INV_TCIN_LOC", "HISTORY_INV_WEEKLY"),
        column_contract=("business_d", "tcin", "location_id", "ending_on_hand_q"),
    )
)

_register(
    LogicalTable(
        name="inventory_weekly_item",
        # Retired *_TCIN rollup family; see sales_weekly_item.
        sql=f"""
SELECT BUSINESS_D AS business_d, TCIN AS tcin, PRIMARY_VENDOR_ID AS primary_vendor_id,
       DEPARTMENT_ID AS department_id, CLASS_ID AS class_id, DPCI AS dpci,
       ITEM_DESCRIPTION AS item_description, LOCATION_TYPE_C AS location_type_c,
       BEGINNING_ON_HAND_Q AS beginning_on_hand_q, ENDING_ON_HAND_Q AS ending_on_hand_q,
       ENDING_ON_HAND_A AS ending_on_hand_a, INSTOCK_PERCENTAGE AS instock_percentage
FROM `{_P}.bpd_raw.weekly_inv_tcin`
""",
        base_tables=(f"{_P}.bpd_raw.weekly_inv_tcin",),
        date_column="business_d",
        patterns=("WEEKLY_INV_TCIN",),
        column_contract=("business_d", "tcin", "ending_on_hand_q"),
    )
)


# ---------- gross margin ----------

_register(
    LogicalTable(
        name="gross_margin",
        # `fiscal_week_end_date AS fiscal_week_end_d` — same load-bearing alias
        # reasoning as inventory's business_d (15 test references), and exactly
        # parsers.CANONICAL_RENAMES' `gross_margin: {"fiscal_week_end_date":
        # "fiscal_week_end_d"}`.
        #
        # The history branch is unioned on a MIN() boundary (backfill sits
        # BEFORE the canvas horizon), where the weekly feeds union on MAX().
        #
        # The `margin` role deliberately resolves to nothing. It resolved to
        # nothing under DuckDB too, no tool queries it, and the real column
        # `adjusted_gross_margin_a` is a dollar amount, not a percentage — do
        # not invent a candidate for it.
        sql=f"""
SELECT fiscal_week_end_date AS fiscal_week_end_d, tcin, dpci, channel_originated,
       location_id_originated, location_id, channel_fulfilled, fulfillment_type,
       fulfillment_subtype, department_id, class_id, item_id,
       net_sales_a, adjusted_gross_margin_a, ytd_adjusted_gross_margin_a,
       adjusted_gross_margin_with_net_ship_margin_a
FROM `{_P}.biom_canvas.fct_target_gross_margin`
WHERE is_current AND NOT is_deleted
UNION ALL
SELECT FISCAL_WEEK_END_DATE, TCIN, DPCI, CHANNEL_ORIGINATED, LOCATION_ID_ORIGINATED, LOCATION_ID,
       CHANNEL_FULFILLED, FULFILLMENT_TYPE, FULFILLMENT_SUBTYPE, DEPARTMENT_ID, CLASS_ID, ITEM_ID,
       NET_SALES_A, ADJUSTED_GROSS_MARGIN_A, YTD_ADJUSTED_GROSS_MARGIN_A,
       ADJUSTED_GROSS_MARGIN_WITH_NET_SHIP_MARGIN_A
FROM `{_P}.bpd_raw.history_gm_weekly`
WHERE FISCAL_WEEK_END_DATE < (SELECT MIN(fiscal_week_end_date)
                              FROM `{_P}.biom_canvas.fct_target_gross_margin`
                              WHERE is_current AND NOT is_deleted)
""",
        base_tables=(
            f"{_P}.biom_canvas.fct_target_gross_margin",
            f"{_P}.bpd_raw.history_gm_weekly",
        ),
        date_column="fiscal_week_end_d",
        patterns=("WEEKLY_GM_TCIN_LOC", "HISTORY_GM_WEEKLY"),
        column_contract=("fiscal_week_end_d", "tcin", "location_id"),
    )
)

_register(
    LogicalTable(
        name="gross_margin_item",
        # Retired *_TCIN rollup family; see sales_weekly_item.
        sql=f"""
SELECT FISCAL_WEEK_END_D AS fiscal_week_end_d, TCIN AS tcin, DPCI AS dpci,
       CHANNEL_ORIGINATED AS channel_originated, CHANNEL_FULFILLED AS channel_fulfilled,
       FULFILLMENT_TYPE AS fulfillment_type, FULFILLMENT_SUBTYPE AS fulfillment_subtype,
       NET_SALES_A AS net_sales_a, NET_SALES_Q AS net_sales_q,
       ADJUSTED_GROSS_MARGIN_A AS adjusted_gross_margin_a
FROM `{_P}.bpd_raw.weekly_gm_tcin`
""",
        base_tables=(f"{_P}.bpd_raw.weekly_gm_tcin",),
        date_column="fiscal_week_end_d",
        patterns=("WEEKLY_GM_TCIN",),
        column_contract=("fiscal_week_end_d", "tcin"),
    )
)


# ---------- orders / PO plan / forecast ----------

_register(
    LogicalTable(
        name="orders_daily",
        # THE ORDER BY MUST BE A TOTAL ORDER. Do not trim it back to
        # `SNAPSHOT_D DESC`: 1,430 (po, tcin, location) groups tie on the latest
        # SNAPSHOT_D, so with no tiebreaker BigQuery picks an arbitrary row and
        # bpd_get_open_orders returns a DIFFERENT headline number on every call
        # (measured, cache off: 497,728 / 502,347 / 504,606 open units across
        # four consecutive runs). With this ORDER BY it is stable at 456,615.
        #
        # The tied rows are receipt-stage duplicates of ONE line, not distinct
        # lines: the same daily drop carries a pre-receipt row (RECEIPT_D '""',
        # ITEM_RECEIVED_Q 0) beside the received row. So we pick the most
        # advanced state (ITEM_RECEIVED_Q DESC) rather than summing — summing
        # exceeds the line's own REVISED_ORDER_Q in 1,311 of the 1,430 groups.
        # TO_JSON_STRING(o) is the final total-order fallback for the 584 groups
        # that are byte-identical duplicates.
        #
        # THE QUALIFY IS THE WHOLE POINT OF THIS ENTRY. FEED_KINDS declares
        # orders_daily 'delta_latest_state' — "the table IS the latest state,
        # query it whole" — which was true of the DuckDB table because the
        # upsert replaced per natural key. In BigQuery the raw feed ACCUMULATES
        # every daily snapshot: 147,166 rows / 14,160,189 open units naive
        # versus 7,710 rows / 497,728 open units latest-state. A 28.4x
        # overstatement if this reduction is ever dropped.
        sql=f"""
SELECT SNAPSHOT_D AS snapshot_d, PURCHASE_ORDER_CREATE_D AS purchase_order_create_d,
       PURCHASE_ORDER_ID AS purchase_order_id, PURCHASE_ORDER_ACTIVE_F AS purchase_order_active_f,
       IMPORT_ORDER_F AS import_order_f, VENDOR_ID AS vendor_id, UPC AS upc, TCIN AS tcin,
       DEPARTMENT_ID AS department_id, CLASS_ID AS class_id, ITEM_ID AS item_id, DPCI AS dpci,
       VENDOR_STYLE_ID AS vendor_style_id, PRODUCT_DESCRIPTION AS product_description,
       RECEIVING_LOCATION_ID AS receiving_location_id,
       RECEIVING_LOCATION_TYPE_C AS receiving_location_type_c,
       ORIGINAL_ORDER_Q AS original_order_q, REVISED_ORDER_Q AS revised_order_q,
       CANCEL_REMAINING_ORDER_Q AS cancel_remaining_order_q,
       ORIGINAL_ESTIMATED_ARRIVAL_D AS original_estimated_arrival_d,
       REVISED_ESTIMATED_ARRIVAL_D AS revised_estimated_arrival_d,
       ITEM_RECEIVED_Q AS item_received_q,
       ITEM_RECEIVED_TOTAL_COST_A AS item_received_total_cost_a,
       ITEM_RECEIVED_TOTAL_RETAIL_A AS item_received_total_retail_a,
       PURCHASE_ORDER_CANCEL_D AS purchase_order_cancel_d,
       PURCHASE_ORDER_CANCELED_F AS purchase_order_canceled_f,
       ON_ORDER_1_WEEK_OUT_Q AS on_order_1_week_out_q,
       ON_ORDER_2_WEEK_OUT_Q AS on_order_2_week_out_q,
       ON_ORDER_3_WEEK_OUT_Q AS on_order_3_week_out_q,
       ON_ORDER_4_8_WEEK_OUT_Q AS on_order_4_8_week_out_q,
       ON_ORDER_9_WEEK_OUT_Q AS on_order_9_week_out_q
FROM `{_P}.bpd_raw.daily_order_tcin_loc` AS o
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY PURCHASE_ORDER_ID, TCIN, RECEIVING_LOCATION_ID
  ORDER BY SNAPSHOT_D DESC, ITEM_RECEIVED_Q DESC, CANCEL_REMAINING_ORDER_Q DESC,
           REVISED_ORDER_Q DESC, ORIGINAL_ORDER_Q DESC, TO_JSON_STRING(o) ASC) = 1
""",
        base_tables=(f"{_P}.bpd_raw.daily_order_tcin_loc",),
        date_column="snapshot_d",
        patterns=("DAILY_ORDER_TCIN_LOC",),
        latest_state_note=(
            "Reduced to the latest snapshot per (purchase_order_id, tcin, "
            "receiving_location_id), breaking same-snapshot ties toward the most "
            "advanced receipt state. The base table accumulates every daily drop, so "
            "its row_count (~147k) is ~19x the logical row count (~7.7k)."
        ),
        column_contract=(
            "snapshot_d",
            "purchase_order_id",
            "tcin",
            "receiving_location_id",
            "revised_order_q",
            "item_received_q",
            "cancel_remaining_order_q",
            "revised_estimated_arrival_d",
            "purchase_order_create_d",
        ),
    )
)

# po_plan_daily / po_plan_biweekly: NO dedup, deliberately.
#
# Both ARE accumulating snapshots (dly_po_plan_tcin: 5,858,400 rows over 118
# distinct BUSINESS_D; bi_weekly_po_planning_item_dc: 9,389,967 over 34), and
# FEED_KINDS already says so. But get_upcoming_pos filters each source to its
# own MAX(business_d) (tools/query.py:933-955). Adding a registry-level
# latest-state reduction here would DOUBLE-APPLY the filter and fight the tool.
# Exactly one layer owns the reduction; for these two it is the tool.
_PO_PLAN_NOTE = (
    "accumulating snapshots by design; get_upcoming_pos filters to MAX(business_d) "
    "per source (tools/query.py:933). Do not add a registry-level latest-state "
    "reduction here — it would double-apply."
)

_register(
    LogicalTable(
        name="po_plan_daily",
        sql=f"""
-- {_PO_PLAN_NOTE}
SELECT BUSINESS_D AS business_d, VENDOR_ID AS vendor_id,
       VENDOR_ORDER_POINT_ID AS vendor_order_point_id, DEPARTMENT_ID AS department_id,
       DPCI AS dpci, TCIN AS tcin, BARCODE_NUMBER AS barcode_number, ORDER_D AS order_d,
       RECEIVING_LOCATION_ID AS receiving_location_id, UNIT_OF_MEASURE_Q AS unit_of_measure_q,
       UNIT_OF_MEASURE_TYPE AS unit_of_measure_type, VENDOR_CASE_PACK_Q AS vendor_case_pack_q,
       NET_STORE_MEAN_DEMAND_Q AS net_store_mean_demand_q, ORDERED_Q AS ordered_q,
       RECEIVED_Q AS received_q, SCHEDULED_RECEIPT_Q AS scheduled_receipt_q,
       BEGINNING_SALESFLOOR_PRESENTATION_UNIT_Q AS beginning_salesfloor_presentation_unit_q,
       ENDING_SALESFLOOR_PRESENTATION_UNIT_Q AS ending_salesfloor_presentation_unit_q
FROM `{_P}.bpd_raw.dly_po_plan_tcin`
""",
        base_tables=(f"{_P}.bpd_raw.dly_po_plan_tcin",),
        date_column="business_d",
        patterns=("DLY_PO_PLAN_TCIN",),
        column_contract=(
            "business_d",
            "order_d",
            "tcin",
            "ordered_q",
            "receiving_location_id",
        ),
    )
)

_register(
    LogicalTable(
        name="po_plan_biweekly",
        sql=f"""
-- {_PO_PLAN_NOTE}
SELECT BUSINESS_D AS business_d, VENDOR_ID AS vendor_id,
       VENDOR_ORDER_POINT_ID AS vendor_order_point_id, DEPARTMENT_ID AS department_id,
       DPCI AS dpci, TCIN AS tcin, BARCODE_NUMBER AS barcode_number, ORDER_D AS order_d,
       RECEIVING_LOCATION_ID AS receiving_location_id, UNIT_OF_MEASURE_Q AS unit_of_measure_q,
       UNIT_OF_MEASURE_TYPE AS unit_of_measure_type, VENDOR_CASE_PACK_Q AS vendor_case_pack_q,
       NET_STORE_MEAN_DEMAND_Q AS net_store_mean_demand_q, ORDERED_Q AS ordered_q,
       RECEIVED_Q AS received_q, SCHEDULED_RECEIPT_Q AS scheduled_receipt_q,
       BEGINNING_SALESFLOOR_PRESENTATION_UNIT_Q AS beginning_salesfloor_presentation_unit_q,
       ENDING_SALESFLOOR_PRESENTATION_UNIT_Q AS ending_salesfloor_presentation_unit_q,
       TOTAL_ORDER_COST_A AS total_order_cost_a, PRODUCT_COST_A AS product_cost_a,
       INITIAL_STORE_INVENTORY_Q AS initial_store_inventory_q,
       PURCHASING_LEAD_TIME_Q AS purchasing_lead_time_q
FROM `{_P}.bpd_raw.bi_weekly_po_planning_item_dc`
""",
        base_tables=(f"{_P}.bpd_raw.bi_weekly_po_planning_item_dc",),
        date_column="business_d",
        patterns=("BI_WEEKLY_PO_PLANNING_ITEM_DC",),
        column_contract=(
            "business_d",
            "order_d",
            "tcin",
            "ordered_q",
            "receiving_location_id",
        ),
    )
)

_register(
    LogicalTable(
        name="forecast_weekly",
        # The QUALIFY reproduces DuckDB's last-write-wins upsert exactly
        # (FEED_KINDS: 'keyed_overwrite_mixed'). Naive: 4,734,013 rows /
        # 6,898,746.57 forecast units. Deduped: 690,143 / 1,055,516.63 — a 6.5x
        # overstatement if dropped. The deduped table still exposes 16 distinct
        # last_update_d values (2026-05-10..2026-07-27), so
        # _classify_forecast_drops keeps working.
        #
        # COST: this CTE scans 151.5 MB and predicate pushdown does NOT survive
        # the QUALIFY — the same 151.5 MB with or without a WHERE. That is why
        # get_forecast_vs_actual must issue ONE job, not two.
        #
        # date_column is the SNAPSHOT stamp; DATE_RANGE_ROLES supplies
        # fiscal_week_begin_d as the forward-looking content horizon.
        sql=f"""
SELECT LAST_UPDATE_D AS last_update_d, TCIN AS tcin, DEPARTMENT_ID AS department_id,
       CLASS_ID AS class_id, DPCI AS dpci, LOCATION_ID AS location_id,
       SELECTED_FORECAST_Q AS selected_forecast_q, VENDOR_ID AS vendor_id,
       FISCAL_WEEK_BEGIN_D AS fiscal_week_begin_d
FROM `{_P}.bpd_raw.dfe_wkly_item_loc_forecast`
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY TCIN, LOCATION_ID, FISCAL_WEEK_BEGIN_D ORDER BY LAST_UPDATE_D DESC) = 1
""",
        base_tables=(f"{_P}.bpd_raw.dfe_wkly_item_loc_forecast",),
        date_column="last_update_d",
        patterns=("DFE_WKLY_ITEM_LOC_FORECAST",),
        latest_state_note=(
            "Reduced to the newest last_update_d per (tcin, location_id, "
            "fiscal_week_begin_d), reproducing the DuckDB keyed-overwrite load. The base "
            "table keeps every drop, so its row_count (~4.7M) is ~6.9x the logical count "
            "(~690k)."
        ),
        column_contract=(
            "last_update_d",
            "fiscal_week_begin_d",
            "tcin",
            "location_id",
            "selected_forecast_q",
        ),
    )
)


# ---------- item / location dimensions ----------
#
# NAMING: the COLUMN_ROLES spelling wins — `item_attr` / `location_attr`, not
# `items` / `locations`. DATASET_KINDS, FEED_KINDS, DATE_RANGE_ROLES,
# schemas.KnownDataset and tests/test_column_roles.py all key off those names.
#
# SOURCING: these are backed by bpd_raw, NOT by biom_canvas.dim_product /
# dim_location. With the bpd_raw sources, item_attr resolves date=
# processed_ct_date and location_attr resolves location=location_number; with
# the dims neither `date` role resolves at all, and dim_product.tcin is STRING
# against INT64 facts.
#
# LOWERCASE ALIASES: resolve_column already lowercases both sides, so aliasing
# is NOT needed to make role resolution work. It is here for a different,
# deliberate reason: ResolvedColumn.name is echoed verbatim into
# extra.date_col, extra.units_col, extra.resolved_columns,
# ColumnNotFound.actual_columns, and becomes the key set of _rows_to_dicts.
# Projecting UPPERCASE would flip the entire public response surface and every
# bpd_run_sql user's column names. The aliases pin the public column contract.

_register(
    LogicalTable(
        name="item_attr",
        sql=f"""
SELECT PROCESSED_CT_DATE AS processed_ct_date, TCIN AS tcin, DPCI AS dpci,
       DEPARTMENT_ID AS department_id, CLASS_ID AS class_id, ITEM_STATE AS item_state,
       MTA_ID AS mta_id, MTA_N AS mta_n, MTA_VALUE_ID AS mta_value_id,
       MTA_VALUE_N AS mta_value_n, MTA_VALUE_UOM AS mta_value_uom,
       ESTORE_ITEM_STATUS_N AS estore_item_status_n, PRIMARY_VENDOR_ID AS primary_vendor_id,
       VENDOR_N AS vendor_n, LAUNCH_UTC_DATE AS launch_utc_date,
       INTENDED_SELLING_CHANNELS_ARR AS intended_selling_channels_arr
FROM `{_P}.bpd_raw.weekly_item_mta`
""",
        base_tables=(f"{_P}.bpd_raw.weekly_item_mta",),
        date_column="processed_ct_date",
        patterns=("WEEKLY_ITEM_MTA",),
        column_contract=("processed_ct_date", "tcin"),
    )
)

_register(
    LogicalTable(
        name="item_attr_extended",
        # Source columns are SPACE-SEPARATED (`ITEM STATE`, `VENDOR ID`, ...),
        # so every reference needs backticks and every alias is
        # _normalize_column_name of the original (lowercase, non-alnum runs to
        # `_`) — the same normalization the DuckDB loader applied to headers.
        #
        # NOTE: item_attr_extended's `date` ROLE resolves to nothing — its
        # COLUMN_ROLES candidates (processed_ct_date, as_of_date, ...) do not
        # exist here. It resolved to nothing under DuckDB too and no tool
        # consumes it. `date_column` below is the detect_date_column answer,
        # which is a different question: last_update_date is the only
        # DATE/TIMESTAMP-typed column that is actually populated (launch_date
        # is a STRING carrying Target's `""` placeholder).
        sql=f"""
SELECT TCIN AS tcin, UPC AS upc, DPCI AS dpci, `ITEM STATE` AS item_state,
       DESCRIPTION AS description, `VENDOR ID` AS vendor_id, `VENDOR NAME` AS vendor_name,
       `BRAND NAME` AS brand_name, `DEPT NO` AS dept_no, `DEPT DESCRIPTION` AS dept_description,
       `CLASS NO` AS class_no, `CLASS DESCRIPTION` AS class_description,
       `PARENT TCIN` AS parent_tcin, `PRODUCT TYPE NAME` AS product_type_name,
       `LAUNCH DATE` AS launch_date, `LAST UPDATE DATE` AS last_update_date
FROM `{_P}.bpd_raw.wkly_tcin_item`
""",
        base_tables=(f"{_P}.bpd_raw.wkly_tcin_item",),
        date_column="last_update_date",
        patterns=("WKLY_TCIN_ITEM",),
        column_contract=("tcin", "last_update_date"),
    )
)

_register(
    LogicalTable(
        name="location_attr",
        # Space-separated source columns again.
        #
        # `last_remodel_date` is a STRING holding Target's literal `""`
        # placeholder in 574 of 2,222 rows alongside real ISO dates. This is the
        # ONE place ResolvedColumn.select_as_date hits a non-DATE column, and it
        # is why that method must emit SAFE_CAST rather than CAST: a plain CAST
        # returns a hard 400 `Invalid date`, and whether it fires is
        # optimizer-dependent (COUNT(CAST(...)) can elide the cast and succeed),
        # so it reads as flaky BigQuery rather than as a translation bug.
        #
        # Because it is a STRING here, `heuristic_date_column` would pick
        # `store_open_date` (the first real DATE) instead. The declaration below
        # is the DuckDB-parity answer and wins; see that function's docstring.
        sql=f"""
SELECT `Location Number` AS location_number, `Location Type` AS location_type,
       `Location Subtype` AS location_subtype, `Location Name` AS location_name,
       City AS city, State AS state, `Zip Code` AS zip_code,
       `Servicing RDC` AS servicing_rdc, `Servicing FDC` AS servicing_fdc,
       Region AS region, District AS district, `Group ID` AS group_id,
       `Store Format` AS store_format, `Store Size` AS store_size,
       `Last Remodel Date` AS last_remodel_date, `Next Remodel Date` AS next_remodel_date,
       `Store Open Date` AS store_open_date, `Store Close Date` AS store_close_date,
       `Store Status` AS store_status, Latitude AS latitude, Longitude AS longitude
FROM `{_P}.bpd_raw.wkly_loc_attr_v0_0`
""",
        base_tables=(f"{_P}.bpd_raw.wkly_loc_attr_v0_0",),
        date_column="last_remodel_date",
        patterns=("WKLY_LOC_ATTR_V0_0",),
        column_contract=("location_number", "last_remodel_date"),
    )
)


# `schemas.KnownDataset` is regenerated from this tuple, and a drift guard
# asserts set(LOGICAL_TABLES) == set(COLUMN_ROLES) == set(DATASET_KINDS)
# == set(FEED_KINDS) == set(KnownDataset.__args__).
KNOWN_DATASET_NAMES: tuple[str, ...] = tuple(LOGICAL_TABLES)


def logical_names() -> frozenset[str]:
    """Every logical table name the tools may reference."""
    return frozenset(LOGICAL_TABLES)


def get(name: str) -> LogicalTable:
    """Registry lookup. Raises KeyError for an unknown logical table."""
    try:
        return LOGICAL_TABLES[name]
    except KeyError:
        raise KeyError(
            f"unknown logical table {name!r}; known: {sorted(LOGICAL_TABLES)}"
        ) from None


def base_datasets(registry: Mapping[str, LogicalTable] | None = None) -> frozenset[str]:
    """BigQuery dataset ids the registry reads from — `{"biom_canvas", "bpd_raw"}` today.

    Used to build the two `__TABLES__` row-count queries (one per dataset).
    """
    reg = LOGICAL_TABLES if registry is None else registry
    out: set[str] = set()
    for t in reg.values():
        for fq in t.base_tables:
            parts = fq.split(".")
            if len(parts) == 3:
                out.add(parts[1])
    return frozenset(out)


def pattern_to_logical(
    registry: Mapping[str, LogicalTable] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Inverse of `LogicalTable.patterns`: ingestion pattern -> logical tables it feeds."""
    reg = LOGICAL_TABLES if registry is None else registry
    out: dict[str, list[str]] = {}
    for t in reg.values():
        for p in t.patterns:
            out.setdefault(p, []).append(t.name)
    return {p: tuple(names) for p, names in out.items()}


# --------------------------------------------------------------------------------------
# CTE injection
# --------------------------------------------------------------------------------------
#
# The service account cannot CREATE VIEW, so the logical tables above are
# materialised per-statement as a `WITH` block prepended to the caller's SQL.
#
# Reference detection is restricted to TABLE POSITION — the identifier directly
# after FROM or JOIN. That is what stops a column alias (`SELECT x AS
# sales_daily`), a string literal, a comment, or a fully-qualified source
# reference from triggering a spurious CTE. The deliberate trade-off: a registry
# name referenced from any OTHER position is not injected, and the query then
# fails loudly with BigQuery's `Unrecognized name: sales_daily`. A loud failure
# is correct here; silently injecting a 151 MB CTE for a name that was meant as
# something else is not.


class CircularDependency(ValueError):
    """A cycle in `LogicalTable.depends_on` (or in bare-name references between bodies)."""


# One tokenizer, scanned left-to-right, so precedence between comments, strings
# and quoted identifiers is decided by whichever STARTS first — the correct rule,
# and the one sequential regex passes get wrong (`'a /* b'` etc.).
_MASK_TOKENS = re.compile(
    r"""
      /\*.*?\*/                            # block comment
    | --[^\n]*                             # line comment
    | \#[^\n]*                             # BigQuery hash comment
    | [rbRB]{0,2}'''(?:\\.|[^\\])*?'''     # triple-quoted string
    | [rbRB]{0,2}\"\"\"(?:\\.|[^\\])*?\"\"\"
    | [rbRB]{0,2}'(?:\\.|[^'\\\n])*'       # single-quoted string
    | [rbRB]{0,2}"(?:\\.|[^"\\\n])*"       # double-quoted string (a STRING in BigQuery)
    | `[^`]*`                              # backtick-quoted identifier
    """,
    re.VERBOSE | re.DOTALL,
)

_CTE_DEF = re.compile(
    r"(?:\bWITH\b(?:\s+RECURSIVE\b)?|,)\s*(?:`(\w+)`|(\w+))\s+AS\s*\(",
    re.IGNORECASE,
)

# The identifier directly after FROM / JOIN. `\b` sits INSIDE the alternation:
# after a closing backtick the surrounding characters are both non-word, so a
# trailing `\b` would reject `` FROM `sales_daily` `` outright.
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+(?:`(\w+)`|(\w+)\b)", re.IGNORECASE)

# Continuation of a comma-separated table list, anchored to the end of the
# previous table reference: an optional alias (with or without AS), then a
# comma, then the next table.
#
# This EXTENDS the "FROM / JOIN only" rule, deliberately: `FROM sales_daily s,
# inventory_daily i` is table position by any reading, and analysts write it in
# bpd_run_sql. Anchoring to the previous match is what keeps it safe — the run
# ends at the first thing that is neither an alias nor a comma, so a SELECT-list
# or GROUP BY comma is never reached. `FROM t GROUP BY x, sales_daily` stops at
# `GROUP BY` (two bare words, no comma) and injects nothing.
_TABLE_LIST_CONT = re.compile(
    r"\s*(?:AS\s+)?(?:`?\w+`?\s*)?,\s*(?:`(\w+)`|(\w+)\b)", re.IGNORECASE
)

_LEAD_WITH_RECURSIVE = re.compile(r"(\s*)WITH\s+RECURSIVE\b", re.IGNORECASE)
_LEAD_WITH = re.compile(r"(\s*)WITH\b", re.IGNORECASE)


def mask_sql(sql: str) -> str:
    """Return a scanning copy of `sql` with non-code spans blanked, same length.

    Length is preserved exactly so offsets found in the mask are valid in the
    original — `inject` relies on that to splice without re-parsing.

    What gets blanked, and with what:

      * comments and string literals -> spaces (newlines kept). They are token
        SEPARATORS, so replacing them with whitespace keeps the surrounding
        tokens adjacent-but-separate, which is what the reference regexes want.
      * backtick-quoted names CONTAINING A DOT -> `!` runs. These are fully
        qualified source refs (`` `proj.bpd_raw.sales_daily` ``) and must not
        read as a reference to the logical table `sales_daily`. `!` rather than
        spaces because `FROM \\`p.d.t\\` AS sales_daily` would otherwise leave
        `FROM   AS ...` and match `AS` as a table name; a non-word, non-space
        filler makes the whole construct unmatchable instead.
      * backtick-quoted names WITHOUT a dot -> kept verbatim. `` FROM `sales_daily` ``
        is a real reference and must still match.
    """
    out: list[str] = []
    pos = 0
    for m in _MASK_TOKENS.finditer(sql):
        out.append(sql[pos : m.start()])
        tok = m.group(0)
        if tok.startswith("`"):
            out.append("!" * len(tok) if "." in tok else tok)
        else:
            out.append("".join("\n" if ch == "\n" else " " for ch in tok))
        pos = m.end()
    out.append(sql[pos:])
    return "".join(out)


def caller_defined_ctes(sql: str) -> frozenset[str]:
    """Names the caller's own `WITH` block defines.

    A caller-defined name SHADOWS the registry: we must not inject a body for
    it, both because the caller clearly meant their own definition and because
    two CTEs with one name is a hard BigQuery error. This is also the escape
    hatch the fixture test harness could use to substitute literal rows for a
    logical table without touching the registry.
    """
    masked = mask_sql(sql)
    return frozenset(m.group(1) or m.group(2) for m in _CTE_DEF.finditer(masked))


def referenced_logical_tables(sql: str, known: frozenset[str]) -> frozenset[str]:
    """Names from `known` that `sql` reads DIRECTLY, in table position.

    Caller-defined CTE names are excluded. This is the single-level scan;
    `resolve_references` walks it to a transitive fixed point through registry
    bodies.
    """
    masked = mask_sql(sql)
    shadowed = frozenset(m.group(1) or m.group(2) for m in _CTE_DEF.finditer(masked))
    found: set[str] = set()
    for m in _TABLE_REF.finditer(masked):
        found.add(m.group(1) or m.group(2))
        pos = m.end()
        while (cont := _TABLE_LIST_CONT.match(masked, pos)) is not None:
            found.add(cont.group(1) or cont.group(2))
            pos = cont.end()
    return frozenset(n for n in found if n in known and n not in shadowed)


def _direct_deps(
    name: str, registry: Mapping[str, LogicalTable], known: frozenset[str]
) -> frozenset[str]:
    """Logical tables a registry body itself depends on.

    `depends_on` is the declared edge set; we ALSO rescan the body defensively so
    a body that references a bare logical name without declaring it still gets
    its dependency injected (and still gets ordered correctly) rather than
    failing at BigQuery with `Unrecognized name`.
    """
    entry = registry[name]
    declared = frozenset(d for d in entry.depends_on if d in known)
    scanned = referenced_logical_tables(entry.sql, known) - {name}
    return declared | scanned


def resolve_references(sql: str, registry: Mapping[str, LogicalTable]) -> frozenset[str]:
    """Every logical table `sql` needs, transitively, minus caller-shadowed names."""
    known = frozenset(registry)
    shadowed = caller_defined_ctes(sql)
    seen: set[str] = set()
    work = list(referenced_logical_tables(sql, known) - shadowed)
    while work:
        name = work.pop()
        if name in seen:
            continue
        seen.add(name)
        for dep in _direct_deps(name, registry, known):
            if dep not in seen and dep not in shadowed:
                work.append(dep)
    return frozenset(seen)


def _topological_order(
    names: frozenset[str], registry: Mapping[str, LogicalTable]
) -> list[str]:
    """Dependencies before dependents; alphabetical tie-break for determinism.

    BigQuery's `WITH` list is sequential — a later CTE may reference an earlier
    one, never the reverse — so the order is a correctness requirement, not a
    cosmetic one. Alphabetical tie-breaking keeps emitted SQL diffable.
    """
    known = frozenset(registry)
    deps = {n: _direct_deps(n, registry, known) & names for n in names}
    emitted: list[str] = []
    done: set[str] = set()
    remaining = set(names)
    while remaining:
        ready = sorted(n for n in remaining if deps[n] <= done)
        if not ready:
            raise CircularDependency(
                f"circular dependency among logical tables: {sorted(remaining)}"
            )
        for n in ready:
            emitted.append(n)
            done.add(n)
        remaining -= set(ready)
    return emitted


def inject(sql: str, bodies: Mapping[str, str]) -> str:
    """Prepend or splice a `WITH` block. `bodies` must already be filtered and ordered.

    Three shapes, all verified to execute:
      * plain statement            -> `WITH <list>\\n<sql>`
      * caller already uses WITH   -> `WITH <list>, <caller's list...>`
      * caller uses WITH RECURSIVE -> `WITH RECURSIVE <list>, <caller's list...>`
        (non-recursive members are legal inside a RECURSIVE list)

    An empty `bodies` returns `sql` byte-identical — never emit an empty `WITH`.
    """
    if not bodies:
        return sql

    parts = ",\n".join(f"{name} AS (\n{body.strip()}\n)" for name, body in bodies.items())
    masked = mask_sql(sql)

    m = _LEAD_WITH_RECURSIVE.match(masked)
    keyword = "WITH RECURSIVE"
    if m is None:
        m = _LEAD_WITH.match(masked)
        keyword = "WITH"
    if m is not None:
        # Splice: keep any leading comment/whitespace, drop the caller's WITH
        # keyword, and comma-join our list in front of theirs.
        lead = sql[: m.end(1)]
        return f"{lead}{keyword} {parts},\n{sql[m.end():].lstrip()}"

    return f"WITH {parts}\n{sql}"


def build_with_report(
    sql: str, registry: Mapping[str, LogicalTable] | None = None
) -> tuple[str, list[str]]:
    """`build`, plus the ordered list of names injected — for cost logging.

    Knowing WHICH logical tables a job pulled in is what makes a surprising byte
    count diagnosable: `forecast_weekly` alone accounts for 151 MB and cannot be
    pruned, so seeing it in the log explains the bill on the spot.
    """
    reg = LOGICAL_TABLES if registry is None else registry
    needed = resolve_references(sql, reg)
    if not needed:
        return sql, []
    order = _topological_order(needed, reg)
    return inject(sql, {n: reg[n].sql for n in order}), order


def build(sql: str, registry: Mapping[str, LogicalTable] | None = None) -> str:
    """The one entry point: referenced -> minus caller-shadowed -> ordered -> injected.

    Called from `BigQueryWarehouse.execute_sql` and nowhere else. See the note
    at that call site for why the injection point is non-negotiable.
    """
    return build_with_report(sql, registry)[0]


# --------------------------------------------------------------------------------------
# Identifier quoting
# --------------------------------------------------------------------------------------


def quote_ident(name: str) -> str:
    """BigQuery identifier quoting. Backticks, NOT double quotes.

    A double-quoted token is a STRING LITERAL in BigQuery, not an identifier:
    `SELECT "sale_quantity"` returns the string 'sale_quantity'. Some call sites
    fail loudly on that (SUM of a string errors), but PARTITION BY / GROUP BY /
    ORDER BY degrade SILENTLY to a constant — a ranked CTE in
    get_inventory_snapshot returned 1 row instead of thousands, with no error
    and no exception. This function is the single highest-priority correctness
    item in the DuckDB -> BigQuery swap.

    The old DuckDB version scrubbed every non-alphanumeric character to `_`.
    That is not kept: it silently corrupts `biom-reporting-s26` into
    `biom_reporting_s26`. Inside backticks every character is safe except a
    backtick, so a backtick is the only thing worth rejecting — and rejecting is
    better than mangling, because a mangled identifier is a wrong answer while a
    raised error is a bug report.

    Never route a table, dataset or project name through this: fully qualified
    source names come from the registry pre-quoted, and logical table names are
    bare CTE names.
    """
    if "`" in name:
        raise ValueError(f"backtick in identifier: {name!r}")
    return f"`{name}`"


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class QueryTooExpensive(RuntimeError):
    """A job was rejected by `maximum_bytes_billed`, or by the pre-flight dry-run gate.

    BigQuery reports a `maximum_bytes_billed` rejection as an HTTP **500**
    (`InternalServerError: Query exceeded limit for bytes billed`), not a 403 —
    so matching on `google.api_core.exceptions.Forbidden` misses it entirely.
    Detection is on the `bytesBilledLimitExceeded` reason string in the message.
    """

    def __init__(self, message: str, *, required_bytes: int | None = None) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes


class CredentialsUnavailable(RuntimeError):
    """No usable BigQuery credential could be resolved. Carries the remediation."""


# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------

_SA_KEY_ENV = "GCP_SA_KEY_B64"
_ADC_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
_SA_KEY_DEST = Path.home() / ".config" / "gcloud" / "biom-bq-sa.json"

_CREDENTIAL_HELP = (
    "BigQuery credentials are unavailable. Provide EITHER:\n"
    f"  * {_ADC_ENV}=/path/to/service-account.json  (an existing key file), or\n"
    f"  * {_SA_KEY_ENV}=<base64 of the service-account JSON>  (materialised to "
    f"{_SA_KEY_DEST} on first use).\n"
    "The expected principal is a read-only service account holding roles/bigquery.dataViewer "
    "and roles/bigquery.jobUser on project " + BQ_PROJECT_DEFAULT + "."
)


def resolve_credentials(*, credentials_path: Path | None = None) -> tuple[Path | None, str]:
    """Make a service-account credential usable by the BigQuery client.

    Resolution order, first hit wins:
      1. an explicit `credentials_path`
      2. an existing, readable `GOOGLE_APPLICATION_CREDENTIALS`
      3. `GCP_SA_KEY_B64`, materialised to `~/.config/gcloud/biom-bq-sa.json`
         with mode 0600 (idempotent)
      4. Application Default Credentials, if the environment happens to have
         them (gcloud login, GCE metadata, Workload Identity)

    Returns `(path_or_None, source_label)`. The label is safe to log and to
    surface in `bpd_bigquery_status`; the KEY BYTES ARE NEVER RETURNED OR
    LOGGED, only the path they were written to.

    Raises `CredentialsUnavailable` with actionable text when nothing works — a
    confusing auth error here is the single biggest time sink for an operator.
    """
    if credentials_path is not None:
        p = Path(credentials_path).expanduser()
        if not p.is_file():
            raise CredentialsUnavailable(
                f"credentials_path {p} does not exist.\n\n{_CREDENTIAL_HELP}"
            )
        os.environ[_ADC_ENV] = str(p)
        return p, "explicit credentials_path"

    existing = os.environ.get(_ADC_ENV)
    if existing:
        p = Path(existing).expanduser()
        if p.is_file():
            return p, f"{_ADC_ENV} env var"
        # Set but wrong: say so rather than silently falling through to a
        # confusing "could not determine project" from ADC further downstream.
        raise CredentialsUnavailable(
            f"{_ADC_ENV} is set to {existing!r} but that file does not exist.\n\n"
            f"{_CREDENTIAL_HELP}"
        )

    b64 = os.environ.get(_SA_KEY_ENV)
    if b64:
        try:
            blob = base64.b64decode(b64, validate=True)
        except Exception as e:  # any decode failure earns the same advice
            raise CredentialsUnavailable(
                f"{_SA_KEY_ENV} is set but is not valid base64 ({e}).\n\n{_CREDENTIAL_HELP}"
            ) from e
        if not blob.lstrip().startswith(b"{"):
            raise CredentialsUnavailable(
                f"{_SA_KEY_ENV} decoded to something that is not JSON. It should be the "
                f"base64 of a service-account key file.\n\n{_CREDENTIAL_HELP}"
            )
        _SA_KEY_DEST.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, and never briefly world-readable. write_bytes() would create the
        # key at the process umask (commonly 0644) and only narrow it on the next
        # line. It is also not atomic, and every concurrent server process targets
        # this same path — two servers starting together could interleave a
        # truncate-and-write against the other's read, which is precisely the
        # cross-process shared-file hazard this migration exists to remove.
        _SA_KEY_DEST.parent.mkdir(parents=True, exist_ok=True)
        _fd, _tmp = tempfile.mkstemp(dir=str(_SA_KEY_DEST.parent), prefix=".sa-key-")
        try:
            os.fchmod(_fd, 0o600)
            with os.fdopen(_fd, "wb") as _fh:
                _fh.write(blob)
            os.replace(_tmp, _SA_KEY_DEST)
        except BaseException:
            Path(_tmp).unlink(missing_ok=True)
            raise
        os.environ[_ADC_ENV] = str(_SA_KEY_DEST)
        return _SA_KEY_DEST, f"{_SA_KEY_ENV} env var (materialised to {_SA_KEY_DEST})"

    # Last resort: let google.auth try ADC. If it has nothing, turn its rather
    # opaque DefaultCredentialsError into the message above.
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])
    except Exception as e:  # normalising every ADC failure mode into one message
        raise CredentialsUnavailable(_CREDENTIAL_HELP) from e
    return None, "application default credentials"


# --------------------------------------------------------------------------------------
# Schema-only date-column heuristic (drift guard for LogicalTable.date_column)
# --------------------------------------------------------------------------------------


def heuristic_date_column(table: str, columns: list[tuple[str, str]]) -> str | None:
    """The old DuckDB `detect_date_column` tiering, minus the value probe.

    Tiers, earliest ordinal wins within each:
      1. a DATE/TIMESTAMP-typed column
      2. a name ending `_date` / `_dt` / `_d`
      3. a name containing date | week | period | as_of | effective
      4. `COLUMN_ROLES[table]["date"]`, consulted LAST so the generic heuristic
         wins for unknown tables

    This is no longer on the query path — `detect_date_column` returns the
    registry's declared `date_column` instead. It survives so a drift test can
    assert each declaration still agrees with what the live schema implies, and
    so a future logical table has a principled default to declare.

    KNOWN LEGITIMATE DIVERGENCE, which a drift test must allow-list rather than
    "fix": `location_attr` declares `last_remodel_date`, while this heuristic
    returns `store_open_date`. The declaration is the DuckDB-parity answer —
    the CSV loader coerced `Last Remodel Date` to a DATE column (mapping
    Target's `""` placeholder to NULL), so it won tier 1 on ordinal position,
    and COLUMN_ROLES' `date` role points at it too. BigQuery keeps the raw
    column as STRING, so tier 1 skips it and lands on the next real DATE.
    Following the heuristic here would silently move every location_attr date
    range.

    The DuckDB implementation had a fifth concern — an all-NULL column must not
    win (item_attr_extended's launch_date) — implemented as a
    `SELECT COUNT(col)` probe per candidate. That probe is deliberately gone:
    on BigQuery it is a full column scan per candidate per call, and declaring
    `date_column` handles the hazard for free.
    """
    if not columns:
        return None
    for name, dtype in columns:
        t = str(dtype).upper()
        if t.startswith(("DATE", "TIMESTAMP")):
            return name
    for name, _ in columns:
        if name.lower().endswith(("_date", "_dt", "_d")):
            return name
    tokens = ("date", "week", "period", "as_of", "effective")
    for name, _ in columns:
        low = name.lower()
        if any(tok in low for tok in tokens):
            return name

    from .column_roles import COLUMN_ROLES

    by_lower = {n.lower(): n for n, _ in columns}
    for candidate in COLUMN_ROLES.get(table, {}).get("date", []):
        real = by_lower.get(candidate.lower())
        if real is not None:
            return real
    return None


# --------------------------------------------------------------------------------------
# The warehouse
# --------------------------------------------------------------------------------------


# A feed is called retired once its newest file is this stale. On today's data
# (2026-09-01) this reproduces parsers.FilePattern.retired exactly: the three
# *_TCIN rollups last landed 2026-05-16 (108 days) and are retired, while
# WEEKLY_ITEM_MTA (35 days) and DFE_WKLY_ITEM_LOC_FORECAST (36 days) are merely
# stale and stay active.
_RETIRED_AFTER_DAYS = 90


class _TTLCache:
    """Trivial single-slot TTL cache. `None` payload means "not populated"."""

    __slots__ = ("_payload", "_stamp", "ttl")

    def __init__(self, ttl_s: float) -> None:
        self.ttl = ttl_s
        self._payload: Any = None
        self._stamp: float = 0.0

    def get(self) -> Any:
        if self._payload is None or (time.monotonic() - self._stamp) > self.ttl:
            return None
        return self._payload

    def put(self, value: Any) -> Any:
        self._payload = value
        self._stamp = time.monotonic()
        return value

    def clear(self) -> None:
        self._payload = None
        self._stamp = 0.0


class BigQueryWarehouse:
    """Read-only BigQuery data layer with the same surface the analytics tools use.

    Replaces the DuckDB `Warehouse` + `ReadOnlyView` pair. `ReadOnlyView` is
    obsolete rather than ported: it wrapped statements in
    `BEGIN TRANSACTION READ ONLY`, whereas here the service account is read-only
    at the CREDENTIAL layer (dataViewer + jobUser; 403 on
    `bigquery.tables.create` for both CREATE VIEW and CREATE TABLE), which is
    strictly stronger and cannot be turned off by a code path.

    Caching, because on per-byte billing this is the difference between free and
    expensive metadata:

      | cache        | source                                  | cost      | TTL              |
      |--------------|-----------------------------------------|-----------|------------------|
      | schema       | dry-run per logical table               | 0 bytes   | process lifetime |
      | row counts   | one `__TABLES__` query per base dataset | 0 bytes   | 300 s            |
      | ingest state | `bpd_meta.ingestion_state` rollup       | ~10 MB    | 300 s            |
      | date ranges  | ONE combined UNION ALL over all tables  | ~527 MB   | 900 s            |

    Only the last costs real money; 900 s bounds a heavy interactive session to
    roughly 4 refreshes an hour. `refresh_metadata()` clears all four.

    Schema caching is a REQUIREMENT, not an optimisation: `get_sell_through`
    alone makes 8 `resolve_column` calls, and the old code's "always query
    information_schema fresh, no caching" rule existed so that an in-process
    sync creating a table would be visible without an MCP restart. There is no
    in-process sync any more — BigQuery schemas change on a pipeline deploy —
    so the reason for that rule is gone with the code that motivated it.
    """

    def __init__(
        self,
        *,
        project: str = BQ_PROJECT_DEFAULT,
        location: str = BQ_LOCATION_DEFAULT,
        registry: Mapping[str, LogicalTable] | None = None,
        client: bigquery.Client | None = None,
        maximum_bytes_billed: int | None = None,
        rowcount_ttl_s: float = 300.0,
        daterange_ttl_s: float = 900.0,
        credentials_path: Path | None = None,
    ) -> None:
        if not location:
            raise ValueError(
                "BigQuery location is required. Omitting it makes INFORMATION_SCHEMA "
                "silently return zero rows, which presents as 'the table has no columns' "
                f"rather than as an error. Use {BQ_LOCATION_DEFAULT!r}."
            )
        self._project = project
        self._location = location
        self._registry: Mapping[str, LogicalTable] = (
            LOGICAL_TABLES if registry is None else registry
        )
        self._max_bytes = maximum_bytes_billed
        self._credentials_source = "injected client"

        if client is None:
            _, self._credentials_source = resolve_credentials(
                credentials_path=credentials_path
            )
            client = bigquery.Client(project=project, location=location)
        self._client = client

        # RLock: describe()/list_datasets() call logical_schema() while already
        # inside a cached section.
        self._lock = threading.RLock()
        self._schema_cache: dict[str, list[tuple[str, str]]] = {}
        self._rowcounts = _TTLCache(rowcount_ttl_s)
        self._ingest = _TTLCache(rowcount_ttl_s)
        self._dateranges = _TTLCache(daterange_ttl_s)
        self._closed = False

    # ---------- identity ----------

    @property
    def read_only(self) -> bool:
        """Always True — the guarantee is at the credential layer, not in a transaction.

        `bpd_run_sql` and `bpd_export_query_to_csv` refuse to run at all unless
        this is truthy, so a falsy value would reject every input with a
        security-shaped message that looks deliberate. There is no setter.
        """
        return True

    @property
    def db_path(self) -> str:
        """Identity string standing in for the old file path. There is no file.

        Type changed from `Path` to `str`; the `.exists()` / `.stat()` callers
        lived only in code being deleted.
        """
        return f"bigquery://{self._project}/{self._location}"

    @property
    def project(self) -> str:
        return self._project

    @property
    def location(self) -> str:
        return self._location

    @property
    def client(self) -> bigquery.Client:
        return self._client

    @property
    def registry(self) -> Mapping[str, LogicalTable]:
        """The logical-table registry this warehouse resolves against.

        `column_roles.table_exists` reads it, and tests swap in a registry whose
        bodies are literal `SELECT ... UNION ALL SELECT ...` fixtures.
        """
        return self._registry

    @property
    def credentials_source(self) -> str:
        """Human-readable provenance of the credential — never the key material."""
        return self._credentials_source

    @property
    def maximum_bytes_billed(self) -> int | None:
        return self._max_bytes

    def close(self) -> None:
        """Idempotent, never raises. Nothing to unlink — the file lock is gone."""
        self._closed = True
        try:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    # ---------- job execution ----------

    def _job_config(self, *, dry_run: bool) -> bigquery.QueryJobConfig:
        cfg = bigquery.QueryJobConfig(use_query_cache=True, dry_run=dry_run)
        # Applied to REAL jobs only: a dry run bills nothing, and setting the
        # cap on it would reject the very estimate we want to read back.
        if not dry_run and self._max_bytes:
            cfg.maximum_bytes_billed = self._max_bytes
        return cfg

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Turn a bytes-billed rejection into `QueryTooExpensive`.

        Matched on the message, NOT on the exception class: BigQuery returns
        HTTP 500 `InternalServerError` for this, so `except Forbidden` never
        fires. The message carries the required byte count, which the caller
        surfaces so the user can widen the cap or narrow the query.
        """
        text = str(exc)
        if "bytesBilledLimitExceeded" not in text:
            return exc
        m = re.search(r"(\d+)\s+or higher required", text)
        return QueryTooExpensive(
            text, required_bytes=int(m.group(1)) if m else None
        )

    def _run(
        self,
        sql: str,
        *,
        dry_run: bool = False,
        injected: tuple[str, ...] | list[str] = (),
    ) -> tuple[QueryJob, RowIterator | None]:
        """Submit one statement. NO CTE INJECTION — callers inject, or don't need to.

        Internal metadata queries (`__TABLES__`, `ingestion_state`) reference no
        logical names, so injection would be a no-op for them; routing them here
        keeps `execute_sql` the single, obvious injection point instead of a
        function that sometimes injects.
        """
        job = self._client.query(
            sql, job_config=self._job_config(dry_run=dry_run), location=self._location
        )
        if dry_run:
            return job, None
        try:
            rows = job.result()
        except Exception as e:  # re-raised, possibly re-typed as QueryTooExpensive
            raise self._translate(e) from e
        logger.info(
            "bq_query",
            bytes_billed=job.total_bytes_billed,
            bytes_processed=job.total_bytes_processed,
            cache_hit=job.cache_hit,
            injected=list(injected),
            job_id=job.job_id,
        )
        return job, rows

    def execute_sql(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run a single SQL statement. Returns (column_names, rows).

        ---------------------------------------------------------------------
        CTE INJECTION HAPPENS HERE, ON THE FINAL SQL STRING, AT THE OUTERMOST
        LEVEL, AND NOWHERE ELSE.
        ---------------------------------------------------------------------
        Two reasons, both structural:

        1. Every analytics tool already emits bare logical-table names
           (`FROM {quote_ident(table)}`, and a literal `FROM forecast_weekly`
           in `_classify_forecast_drops`). Centralising injection here means
           `tools/query.py` needs zero injection awareness.

        2. It resolves the `wrap_with_limit` ordering hazard automatically.
           `bpd_run_sql` wraps user SQL as
           `SELECT * FROM (<user sql>) AS _bpd_sub LIMIT n` BEFORE calling us,
           so injecting here puts the `WITH` outermost, in scope for the whole
           statement including inside the subquery. Injecting before wrapping
           would bury the CTEs inside `_bpd_sub` — which still WORKS today,
           because nothing references a logical table outside the wrapper, so
           it would pass every test and break the first time someone adds a
           clause outside it.

        Rows come back as plain positional tuples, not `bigquery.Row`: every
        caller indexes `r[0]` / `r[1]`, tuple-unpacks, or feeds `_rows_to_dicts`,
        and a `Row` is not a drop-in for that.
        """
        final, injected = build_with_report(sql, self._registry)
        _job, rows = self._run(final, injected=injected)
        if rows is None:  # pragma: no cover - only reachable with dry_run=True
            return [], []
        schema = list(rows.schema or [])
        if not schema:
            # Mirrors the old `if cur.description` branch: a statement with no
            # projected columns yields no names and no rows.
            return [], []
        return [f.name for f in schema], [tuple(r.values()) for r in rows]

    def dry_run(self, sql: str) -> QueryJob:
        """Validate and price a statement without executing it. Costs 0 bytes.

        Injects CTEs exactly as `execute_sql` does, so the thing validated is
        the thing that would run. This replaces the DuckDB `EXPLAIN` gate:
        BigQuery rejects `EXPLAIN` outright (`Statement not supported:
        ExplainStatement`), and a dry run is strictly better anyway — it keeps
        the syntax/name validation AND adds `total_bytes_processed`, which
        matters far more on per-byte billing than it did on a local file.
        """
        final, injected = build_with_report(sql, self._registry)
        job, _ = self._run(final, dry_run=True, injected=injected)
        return job

    # ---------- schema ----------

    def logical_schema(self, table: str) -> list[tuple[str, str]]:
        """`[(column_name, BIGQUERY_TYPE)]` in projection order, cached, 0 bytes on a miss.

        Derived from a `dry_run` of `SELECT * FROM (<body>) LIMIT 0` rather than
        from a declared column list, for two reasons: a hand-written list drifts
        the first time a source column is added, and deriving it means a test
        that swaps a fixture body into the registry resolves roles against the
        FIXTURE's real schema.

        `INFORMATION_SCHEMA` cannot answer this question at all — a CTE-injected
        logical table appears in no catalogue anywhere (verified: querying
        `biom_canvas.INFORMATION_SCHEMA.COLUMNS WHERE table_name='sales_daily'`
        returns zero rows even with the CTE present) — and it would bill a 10 MB
        minimum per call besides.

        Raises KeyError for a name that is not in the registry.
        """
        with self._lock:
            hit = self._schema_cache.get(table)
            if hit is not None:
                return list(hit)
        if table not in self._registry:
            raise KeyError(
                f"unknown logical table {table!r}; known: {sorted(self._registry)}"
            )
        body = self._registry[table].sql
        job, _ = self._run(f"SELECT * FROM (\n{body.strip()}\n) LIMIT 0", dry_run=True)
        cols = [(f.name, str(f.field_type).upper()) for f in (job.schema or [])]
        with self._lock:
            self._schema_cache[table] = cols
        return list(cols)

    def detect_date_column(self, table: str) -> str | None:
        """The table's date column — DECLARED by the registry, not probed.

        The DuckDB implementation ran a `SELECT COUNT(col)` per candidate column
        per call (the all-NULL guard added after item_attr_extended's launch_date
        made every listing report a null range). Free on a local file; a full
        column scan each on BigQuery, fanned out once per dataset from
        `list_datasets`. Declaring the answer removes the cost AND the hazard.

        `heuristic_date_column()` retains the schema-only tiering rules for the
        drift test that asserts the declarations still match reality.

        Returns None for a name that is not a logical table.
        """
        entry = self._registry.get(table)
        return entry.date_column if entry is not None else None

    def describe(self) -> dict[str, Any]:
        """Schema of every logical table, for `bpd_describe_schema` and `bpd://schema`.

        Shape: `{"tables": {name: {...}}, "views": []}`.

        `"views"` stays as a key with an empty list even though BigQuery
        exposes none: `describe_schema` reads `info["views"]` unconditionally,
        and dropping it would raise KeyError inside the tool most likely to be
        used as a first smoke test.

        Logical tables are never enumerated from a catalogue — they are CTEs and
        appear in none. The registry IS the catalogue, and entries come back in
        registry order.

        `row_count` IS A DELIBERATE BEHAVIOUR CHANGE. It is now the PRIMARY BASE
        TABLE's row count from `__TABLES__` (0 bytes), not `COUNT(*)` through
        the CTE: counting through all 15 CTEs costs ~333 MB per describe() call,
        and admin.py calls describe() inside a loop. The count therefore
        OVERSTATES every filtered or deduped table — orders_daily reports
        ~147k base rows against ~7.7k logical rows. `row_count_basis` and
        `latest_state_note` are what make that legible, so a renderer must
        surface both.
        """
        counts = self._base_row_counts()
        out: dict[str, Any] = {"tables": {}, "views": []}
        for name, entry in self._registry.items():
            cols = [{"name": n, "type": t} for n, t in self.logical_schema(name)]
            primary = entry.primary_base_table
            out["tables"][name] = {
                "columns": cols,
                "row_count": counts.get(primary, 0),
                "row_count_basis": "base_table",
                "source": primary,
                "latest_state_note": entry.latest_state_note,
            }
        return out

    def refresh_metadata(self) -> None:
        """Drop the schema, row-count, ingest-state and date-range caches.

        Called by `bpd_health_check` so an operator can force re-introspection
        after a pipeline deploy without restarting the MCP server.
        """
        with self._lock:
            self._schema_cache.clear()
            self._rowcounts.clear()
            self._ingest.clear()
            self._dateranges.clear()

    # ---------- metadata queries ----------

    def base_row_counts(self) -> dict[str, int]:
        """Public alias for `_base_row_counts`.

        `column_roles.validate_roles` needs a cheap "is this table empty?"
        probe. Its DuckDB form was `SELECT COUNT(*) FROM {dataset}`, which
        against the registry roster would cost roughly 333 MB per health check.
        Route it through here (or through `describe()[t]["row_count"]`) instead:
        both read `__TABLES__` and bill nothing.
        """
        return self._base_row_counts()

    def _base_row_counts(self) -> dict[str, int]:
        """`{fully_qualified_base_table: row_count}` from `__TABLES__`. 0 bytes.

        `__TABLES__` is the cheap catalogue: it bills nothing, where any
        `INFORMATION_SCHEMA` query bills a 10 MB minimum and
        `region-*.INFORMATION_SCHEMA.TABLE_STORAGE` additionally returns 20+
        `_stage_*` rows that would have to be filtered out.
        """
        with self._lock:
            hit = self._rowcounts.get()
            if hit is not None:
                return dict(hit)

        wanted = {
            fq for t in self._registry.values() for fq in t.base_tables if fq.count(".") == 2
        }
        datasets = sorted({fq.split(".")[1] for fq in wanted})
        counts: dict[str, int] = {}
        if datasets:
            union = "\nUNION ALL\n".join(
                f"SELECT project_id, dataset_id, table_id, row_count "
                f"FROM `{self._project}.{ds}.__TABLES__`"
                for ds in datasets
            )
            _, it = self._run(union)
            for r in it or []:
                counts[f"{r[0]}.{r[1]}.{r[2]}"] = int(r[3])

        with self._lock:
            self._rowcounts.put(counts)
        return dict(counts)

    def _ingest_rollup(self) -> dict[str, dict[str, Any]]:
        """Per-pattern rollup of `bpd_meta.ingestion_state`, keyed by pattern.

        This is the pipeline's own ledger of files landed in GCS/BigQuery: 834
        rows, 18 patterns. It is the only honest BigQuery analogue of the old
        `_file_ledger`, and it answers a subtly different question — it means
        "a file arrived", not "rows are queryable". Callers that report
        freshness must cross-check against the date ranges before claiming a
        dataset is current.
        """
        with self._lock:
            hit = self._ingest.get()
            if hit is not None:
                return dict(hit)

        sql = f"""
SELECT pattern,
       COUNT(*)                                        AS files,
       MAX(file_date)                                  AS max_file_date,
       MIN(file_date)                                  AS min_file_date,
       MAX(downloaded_at)                              AS max_downloaded_at,
       COALESCE(SUM(size_bytes), 0)                    AS total_bytes,
       DATE_DIFF(CURRENT_DATE(), MAX(file_date), DAY)  AS lag_days
FROM `{self._project}.{BQ_META_DATASET}.{BQ_INGESTION_STATE}`
GROUP BY pattern
"""
        _, it = self._run(sql)
        out = {
            r["pattern"]: {
                "pattern": r["pattern"],
                "files": int(r["files"]),
                "max_file_date": r["max_file_date"],
                "min_file_date": r["min_file_date"],
                "max_downloaded_at": r["max_downloaded_at"],
                "total_bytes": int(r["total_bytes"]),
                "lag_days": r["lag_days"],
            }
            for r in (it or [])
        }
        with self._lock:
            self._ingest.put(out)
        return dict(out)

    def _date_ranges(self) -> dict[str, dict[str, Any]]:
        """Snapshot + content date extents for every logical table, in ONE job.

        This is the only metadata query that costs real money: ~527 MB for the
        combined form, versus ~618 MB if issued as 15 separate jobs. Never issue
        the per-table form. The 900 s TTL is worth far more than the 15% the
        combining saves.

        Every extent goes through `SAFE_CAST(col AS DATE)` because the extents
        are taken over STRING date columns too — `location_attr.last_remodel_date`
        holds Target's `""` placeholder in 574 of 2,222 rows, and a plain CAST
        there is a hard 400.
        """
        with self._lock:
            hit = self._dateranges.get()
            if hit is not None:
                return dict(hit)

        from .column_roles import DATE_RANGE_ROLES, ColumnNotFound, resolve_column

        branches: list[str] = []
        columns: dict[str, tuple[str | None, str | None]] = {}
        for name in self._registry:
            snapshot_col: str | None = None
            content_col: str | None = None
            roles_map = DATE_RANGE_ROLES.get(name)
            if roles_map:
                for key, target in (("snapshot", "snapshot"), ("content", "content")):
                    try:
                        resolved = resolve_column(self, name, roles_map[key]).name
                    except ColumnNotFound:
                        resolved = None
                    if target == "snapshot":
                        snapshot_col = resolved
                    else:
                        content_col = resolved
            if snapshot_col is None:
                snapshot_col = self.detect_date_column(name)
            if content_col is None:
                content_col = snapshot_col
            columns[name] = (snapshot_col, content_col)

            def _extent(col: str | None) -> tuple[str, str]:
                if col is None:
                    return "CAST(NULL AS DATE)", "CAST(NULL AS DATE)"
                expr = f"SAFE_CAST({quote_ident(col)} AS DATE)"
                return f"MIN({expr})", f"MAX({expr})"

            snap_min, snap_max = _extent(snapshot_col)
            cont_min, cont_max = _extent(content_col)
            branches.append(
                f"SELECT '{name}' AS logical_table, {snap_min} AS snap_min, "
                f"{snap_max} AS snap_max, {cont_min} AS content_min, "
                f"{cont_max} AS content_max FROM {name}"
            )

        out: dict[str, dict[str, Any]] = {}
        if branches:
            cols, rows = self.execute_sql("\nUNION ALL\n".join(branches))
            idx = {c: i for i, c in enumerate(cols)}
            for row in rows:
                name = row[idx["logical_table"]]
                snapshot_col, content_col = columns[name]
                out[name] = {
                    "date_column": snapshot_col,
                    "content_column": content_col,
                    "min_date": row[idx["snap_min"]],
                    "max_date": row[idx["snap_max"]],
                    "content_min_date": row[idx["content_min"]],
                    "content_max_date": row[idx["content_max"]],
                }

        with self._lock:
            self._dateranges.put(out)
        return dict(out)

    # ---------- listings ----------

    def list_datasets(self) -> list[dict[str, Any]]:
        """One row per logical table with summary stats.

        Every key the DuckDB version returned is preserved, because
        `bpd_list_datasets` and the freshness tool read them by name:
        `dataset, feed_kind, status, row_count, date_column, min_date, max_date,
        content_column, content_min_date, content_max_date, file_count,
        last_loaded_at`.

        What changed underneath:

          * `status` is derived from feed freshness (retired if the newest
            `file_date` across this table's `patterns` is more than
            `_RETIRED_AFTER_DAYS` old) rather than read off `FilePattern.retired`,
            which dies with parsers.py. On today's data this reproduces the old
            flags exactly, including sales_weekly staying ACTIVE because its
            live weekly pattern outranks its retired HISTORY twin.
          * `file_count` / `last_loaded_at` come from `bpd_meta.ingestion_state`
            rather than the local `_file_ledger`. They are the honest analogue
            and they mean "files downloaded", not "rows queryable" — returning
            None/0 instead would throw away an answer we actually have.
          * `row_count` is the primary base table's `__TABLES__` count; see
            `describe()` for why, and for how it overstates deduped tables.

        The old `dict.fromkeys` dedupe is not needed any more, but its INTENT is
        (`test_list_datasets_dedupes_multi_pattern_datasets`): one row per
        logical table even when several ingestion patterns feed it. The reason
        changed — it used to be "two FilePatterns map to one dataset", it is now
        "one LogicalTable lists several `patterns`" — so the invariant is
        preserved structurally, by iterating the registry.
        """
        from .column_roles import FEED_KINDS

        counts = self._base_row_counts()
        ingest = self._ingest_rollup()
        ranges = self._date_ranges()

        results: list[dict[str, Any]] = []
        for name, entry in self._registry.items():
            rng = ranges.get(name, {})
            files = 0
            last_loaded = None
            newest_file_date = None
            for pattern in entry.patterns:
                stat = ingest.get(pattern)
                if stat is None:
                    continue
                files += stat["files"]
                if stat["max_downloaded_at"] is not None and (
                    last_loaded is None or stat["max_downloaded_at"] > last_loaded
                ):
                    last_loaded = stat["max_downloaded_at"]
                if stat["max_file_date"] is not None and (
                    newest_file_date is None or stat["max_file_date"] > newest_file_date
                ):
                    newest_file_date = stat["max_file_date"]

            lag = None
            if newest_file_date is not None:
                lags = [
                    ingest[p]["lag_days"]
                    for p in entry.patterns
                    if p in ingest and ingest[p]["max_file_date"] == newest_file_date
                ]
                lag = min(lags) if lags else None
            status = "retired" if (lag is not None and lag > _RETIRED_AFTER_DAYS) else "active"

            results.append(
                {
                    "dataset": name,
                    "feed_kind": FEED_KINDS.get(name, "unknown"),
                    "status": status,
                    "row_count": counts.get(entry.primary_base_table, 0),
                    "date_column": rng.get("date_column") or entry.date_column,
                    "min_date": rng.get("min_date"),
                    "max_date": rng.get("max_date"),
                    "content_column": rng.get("content_column") or entry.date_column,
                    "content_min_date": rng.get("content_min_date"),
                    "content_max_date": rng.get("content_max_date"),
                    "file_count": files,
                    "last_loaded_at": last_loaded,
                }
            )
        return results

    def freshness_stats(self) -> dict[str, Any]:
        """Pipeline freshness from `bpd_meta.ingestion_state`. Replaces `disk_stats()`.

        `disk_stats()` reported bytes on disk and a local sync-log timestamp;
        neither concept survives. What the old `bpd_cache_status` tool was
        actually FOR — "how current is this data?" — does survive, and this is
        where it now comes from.

        Returns::

            {"per_pattern": [{"pattern", "files", "max_file_date",
                              "max_downloaded_at", "total_bytes", "lag_days",
                              "logical_tables"}],
             "last_ingest_at": datetime | None,
             "total_files": int,
             "patterns_seen": int}

        `max_downloaded_at` means a FILE ARRIVED, not that rows are queryable.
        Cross-check against `list_datasets()`'s `max_date` before telling a user
        a dataset is current.
        """
        rollup = self._ingest_rollup()
        inverse = pattern_to_logical(self._registry)
        per_pattern = [
            {**stat, "logical_tables": list(inverse.get(pattern, ()))}
            for pattern, stat in sorted(rollup.items())
        ]
        downloads = [s["max_downloaded_at"] for s in rollup.values() if s["max_downloaded_at"]]
        return {
            "per_pattern": per_pattern,
            "last_ingest_at": max(downloads) if downloads else None,
            "total_files": sum(s["files"] for s in rollup.values()),
            "patterns_seen": len(rollup),
        }


# Back-compat alias. `tools/query.py` and `tools/admin.py` annotate
# `warehouse: Warehouse`; keeping the name resolvable keeps their diffs to
# actual behaviour changes. `warehouse.py` re-exports both.
Warehouse = BigQueryWarehouse
