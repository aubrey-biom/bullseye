"""Query tools: run_sql, sales_summary, top_skus, inventory_snapshot, sell_through,
describe_schema, plus the S&OP analytics added in the May 2026 patch
(open_orders, upcoming_pos, forecast_vs_actual)."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..formatting import (
    make_error_response,
    make_kv_response,
    make_table_response,
)
from ..schemas import (
    DescribeSchemaInput,
    ForecastVsActualInput,
    InventorySnapshotInput,
    OpenOrdersInput,
    RunSqlInput,
    SalesSummaryInput,
    SellThroughInput,
    ToolResponse,
    TopSkusInput,
    UpcomingPosInput,
)
from ..sql_safety import SqlBlocked, validate, wrap_with_limit
from ..warehouse import Warehouse, quote_ident


def _rows_to_dicts(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(cols, r, strict=False)) for r in rows]


# ---------- bpd_run_sql ----------


async def run_sql(read_only_warehouse: Warehouse, params: RunSqlInput) -> ToolResponse:
    if not read_only_warehouse.read_only:
        # Belt-and-suspenders: refuse to ever run on a writable connection.
        return make_error_response(
            code="SQL_BLOCKED",
            message="bpd_run_sql may only execute against a read-only warehouse connection",
            fmt=params.response_format,
        )

    try:
        cleaned = validate(params.sql)
    except SqlBlocked as e:
        return make_error_response(
            code="SQL_BLOCKED",
            message=str(e),
            details={"sql": params.sql[:500]},
            fmt=params.response_format,
        )

    wrapped = wrap_with_limit(cleaned, params.limit)
    # Step 1: EXPLAIN to ensure planner accepts it before we execute.
    try:
        read_only_warehouse.execute_sql(f"EXPLAIN {wrapped}")
    except Exception as e:
        return make_error_response(
            code="SQL_PLAN_FAILED",
            message=f"EXPLAIN failed: {e}",
            fmt=params.response_format,
        )

    try:
        cols, rows = read_only_warehouse.execute_sql(wrapped)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            fmt=params.response_format,
        )
    dict_rows = _rows_to_dicts(cols, rows)
    return make_table_response(
        rows=dict_rows,
        columns=cols if cols else None,
        title="Query results",
        extra={"row_count": len(dict_rows), "columns": cols, "limit": params.limit},
        fmt=params.response_format,
    )


# ---------- bpd_describe_schema ----------


async def describe_schema(warehouse: Warehouse, params: DescribeSchemaInput) -> ToolResponse:
    info = warehouse.describe()
    if params.response_format == "json":
        return make_kv_response(data=info, title="Warehouse schema", fmt="json")
    # Render each table as a sub-table.
    parts: list[str] = ["### Warehouse schema"]
    if info["views"]:
        parts.append("**Views**: " + ", ".join(info["views"]))
    for name, body in info["tables"].items():
        parts.append(f"\n#### `{name}` ({body['row_count']:,} rows)")
        col_rows = [{"name": c["name"], "type": c["type"]} for c in body["columns"]]
        from ..formatting import render_markdown_table

        parts.append(render_markdown_table(col_rows, columns=["name", "type"]))
    rendered = "\n\n".join(parts)
    from ..schemas import ToolResponse as _TR

    return _TR(
        ok=True,
        format="markdown",
        rendered=rendered,
        data=info,
    )


# ---------- bpd_get_sales_summary ----------


def _sales_table_for(warehouse: Warehouse, grain: str) -> tuple[str, str, str, str] | None:
    """Return (table_name, date_col, units_col, dollars_col) if available.

    Uses sales_daily for grain=day, sales_weekly otherwise. Resolves date and metric
    columns from whatever schema is present in the discovered tables.
    """
    desc = warehouse.describe()["tables"]
    table = "sales_daily" if grain == "day" else "sales_weekly"
    if table not in desc:
        # Fallback to whichever is present.
        if "sales_weekly" in desc:
            table = "sales_weekly"
        elif "sales_daily" in desc:
            table = "sales_daily"
        else:
            return None
    cols = {c["name"]: c["type"] for c in desc[table]["columns"]}
    date_col = next((c for c in ("sale_date", "week_end_date", "snapshot_date") if c in cols), None)
    units_col = next(
        (c for c in ("units_sold", "units", "unit_count", "sales_units") if c in cols),
        None,
    )
    dollars_col = next(
        (
            c
            for c in (
                "sales_dollars",
                "sales_amt",
                "sales_amount",
                "dollars",
                "sales",
                "gross_sales_amt",
            )
            if c in cols
        ),
        None,
    )
    if date_col is None or units_col is None:
        return None
    return table, date_col, units_col, (dollars_col or units_col)  # dollars_col may be None


async def get_sales_summary(
    warehouse: Warehouse, params: SalesSummaryInput
) -> ToolResponse:
    spec = _sales_table_for(warehouse, params.grain)
    if not spec:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="No sales_daily or sales_weekly table loaded yet — run bpd_sync_new_files first.",
            fmt=params.response_format,
        )
    table, date_col, units_col, dollars_col = spec

    if params.grain == "day":
        bucket = quote_ident(date_col)
    elif params.grain == "week":
        bucket = f"date_trunc('week', {quote_ident(date_col)})"
    else:
        bucket = f"date_trunc('month', {quote_ident(date_col)})"

    where_clauses: list[str] = []
    if params.start_date:
        where_clauses.append(f"{quote_ident(date_col)} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_clauses.append(f"{quote_ident(date_col)} <= DATE '{params.end_date.isoformat()}'")
    if params.tcin is not None:
        where_clauses.append(f"tcin = {int(params.tcin)}")
    if params.location_id is not None:
        # Try a few candidates and OR them. Cheap and forgiving.
        loc_options = []
        for c in ("location_id", "store_id", "loc_id"):
            if c in {col["name"] for col in warehouse.describe()["tables"][table]["columns"]}:
                loc_options.append(f"{quote_ident(c)} = {int(params.location_id)}")
        if loc_options:
            where_clauses.append("(" + " OR ".join(loc_options) + ")")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    same_metric = units_col == dollars_col

    if same_metric:
        sql = (
            f"SELECT {bucket} AS bucket, "
            f"SUM({quote_ident(units_col)}) AS total_units "
            f"FROM {quote_ident(table)} {where_sql} "
            "GROUP BY bucket ORDER BY bucket"
        )
    else:
        sql = (
            f"SELECT {bucket} AS bucket, "
            f"SUM({quote_ident(units_col)}) AS total_units, "
            f"SUM({quote_ident(dollars_col)}) AS total_dollars "
            f"FROM {quote_ident(table)} {where_sql} "
            "GROUP BY bucket ORDER BY bucket"
        )

    try:
        cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )

    dict_rows = _rows_to_dicts(cols, rows)
    return make_table_response(
        rows=dict_rows,
        columns=cols,
        title=f"Sales summary ({params.grain}, table={table})",
        extra={
            "table": table,
            "date_col": date_col,
            "units_col": units_col,
            "dollars_col": dollars_col if not same_metric else None,
            "sql": sql,
        },
        fmt=params.response_format,
    )


# ---------- bpd_get_top_skus ----------


async def get_top_skus(warehouse: Warehouse, params: TopSkusInput) -> ToolResponse:
    spec = _sales_table_for(warehouse, "week")  # weekly is canonical
    if not spec:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="No sales table loaded yet.",
            fmt=params.response_format,
        )
    table, date_col, units_col, dollars_col = spec
    metric_col = dollars_col if params.by == "dollars" else units_col

    where_clauses: list[str] = []
    if params.start_date:
        where_clauses.append(f"{quote_ident(date_col)} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_clauses.append(f"{quote_ident(date_col)} <= DATE '{params.end_date.isoformat()}'")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = (
        "SELECT tcin, "
        f"SUM({quote_ident(metric_col)}) AS metric_total "
        f"FROM {quote_ident(table)} {where_sql} "
        "GROUP BY tcin ORDER BY metric_total DESC NULLS LAST "
        f"LIMIT {int(params.top_n)}"
    )
    try:
        cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )
    dict_rows = _rows_to_dicts(cols, rows)
    return make_table_response(
        rows=dict_rows,
        columns=cols,
        title=f"Top {params.top_n} SKUs by {params.by}",
        extra={"metric_col": metric_col, "sql": sql},
        fmt=params.response_format,
    )


# ---------- bpd_get_inventory_snapshot ----------


def _inventory_table(warehouse: Warehouse) -> tuple[str, str, str] | None:
    """Returns (table, date_col, on_hand_col) for inventory queries."""
    desc = warehouse.describe()["tables"]
    for table in ("inventory_daily", "inventory_weekly"):
        if table not in desc:
            continue
        cols = {c["name"] for c in desc[table]["columns"]}
        date_col = next(
            (c for c in ("snapshot_date", "week_end_date", "sale_date") if c in cols), None
        )
        on_hand_col = next(
            (
                c
                for c in ("on_hand_units", "inv_units", "on_hand", "stock_units", "qty_on_hand")
                if c in cols
            ),
            None,
        )
        if date_col and on_hand_col:
            return table, date_col, on_hand_col
    return None


async def get_inventory_snapshot(
    warehouse: Warehouse, params: InventorySnapshotInput
) -> ToolResponse:
    spec = _inventory_table(warehouse)
    if not spec:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="No inventory table loaded yet.",
            fmt=params.response_format,
        )
    table, date_col, on_hand_col = spec
    as_of = params.as_of or date.today()

    where: list[str] = [f"{quote_ident(date_col)} <= DATE '{as_of.isoformat()}'"]
    if params.tcin is not None:
        where.append(f"tcin = {int(params.tcin)}")

    loc_filter = ""
    if params.location_id is not None:
        cols = {c["name"] for c in warehouse.describe()["tables"][table]["columns"]}
        loc_options = []
        for c in ("location_id", "store_id", "loc_id"):
            if c in cols:
                loc_options.append(f"{quote_ident(c)} = {int(params.location_id)}")
        if loc_options:
            loc_filter = " AND (" + " OR ".join(loc_options) + ")"

    cols_in_table = {c["name"] for c in warehouse.describe()["tables"][table]["columns"]}
    pk_loc = next((c for c in ("location_id", "store_id", "loc_id") if c in cols_in_table), None)
    if pk_loc is None:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=f"{table} has no location_id/store_id/loc_id column",
            fmt=params.response_format,
        )

    sql = f"""
        WITH ranked AS (
            SELECT tcin, {quote_ident(pk_loc)} AS location_id, {quote_ident(date_col)} AS dt,
                   {quote_ident(on_hand_col)} AS on_hand,
                   ROW_NUMBER() OVER (PARTITION BY tcin, {quote_ident(pk_loc)}
                                      ORDER BY {quote_ident(date_col)} DESC) AS rn
            FROM {quote_ident(table)}
            WHERE {' AND '.join(where)}{loc_filter}
        )
        SELECT tcin, location_id, dt AS as_of_date, on_hand
        FROM ranked WHERE rn = 1
        ORDER BY tcin, location_id
        LIMIT {int(params.limit)}
    """
    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=f"Inventory snapshot as of {as_of.isoformat()} (table={table})",
        extra={"table": table, "date_col": date_col, "on_hand_col": on_hand_col},
        fmt=params.response_format,
    )


# ---------- bpd_get_sell_through ----------


async def get_sell_through(warehouse: Warehouse, params: SellThroughInput) -> ToolResponse:
    sales_spec = _sales_table_for(warehouse, "week")
    inv_spec = _inventory_table(warehouse)
    if not sales_spec or not inv_spec:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="Need both sales_weekly and an inventory table loaded.",
            fmt=params.response_format,
        )
    sales_table, sales_date, sales_units, _ = sales_spec
    inv_table, inv_date, inv_on_hand = inv_spec

    sales_cols = {c["name"] for c in warehouse.describe()["tables"][sales_table]["columns"]}
    inv_cols = {c["name"] for c in warehouse.describe()["tables"][inv_table]["columns"]}
    sales_loc = next((c for c in ("location_id", "store_id", "loc_id") if c in sales_cols), None)
    inv_loc = next((c for c in ("location_id", "store_id", "loc_id") if c in inv_cols), None)
    if sales_loc is None or inv_loc is None:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message="missing location column on sales or inventory",
            fmt=params.response_format,
        )

    where_sales: list[str] = []
    if params.start_date:
        where_sales.append(f"{quote_ident(sales_date)} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_sales.append(f"{quote_ident(sales_date)} <= DATE '{params.end_date.isoformat()}'")
    if params.tcin is not None:
        where_sales.append(f"tcin = {int(params.tcin)}")
    if params.location_id is not None:
        where_sales.append(f"{quote_ident(sales_loc)} = {int(params.location_id)}")
    where_sales_sql = ("WHERE " + " AND ".join(where_sales)) if where_sales else ""

    sql = f"""
        WITH s AS (
            SELECT tcin, {quote_ident(sales_loc)} AS location_id,
                   SUM({quote_ident(sales_units)}) AS units_sold,
                   COUNT(DISTINCT {quote_ident(sales_date)}) AS weeks_observed
            FROM {quote_ident(sales_table)}
            {where_sales_sql}
            GROUP BY tcin, {quote_ident(sales_loc)}
        ),
        latest_inv AS (
            SELECT tcin, {quote_ident(inv_loc)} AS location_id, {quote_ident(inv_on_hand)} AS on_hand
            FROM (
                SELECT tcin, {quote_ident(inv_loc)}, {quote_ident(inv_on_hand)},
                       ROW_NUMBER() OVER (PARTITION BY tcin, {quote_ident(inv_loc)}
                                          ORDER BY {quote_ident(inv_date)} DESC) AS rn
                FROM {quote_ident(inv_table)}
            ) WHERE rn = 1
        )
        SELECT s.tcin, s.location_id, s.units_sold, latest_inv.on_hand,
               CASE WHEN s.units_sold IS NULL OR s.units_sold = 0 THEN NULL
                    ELSE (latest_inv.on_hand * 1.0)
                         / NULLIF(s.units_sold / NULLIF(s.weeks_observed, 0), 0)
               END AS weeks_of_supply,
               CASE WHEN (s.units_sold + COALESCE(latest_inv.on_hand, 0)) = 0 THEN NULL
                    ELSE s.units_sold * 1.0
                         / (s.units_sold + COALESCE(latest_inv.on_hand, 0))
               END AS sell_through_rate
        FROM s LEFT JOIN latest_inv USING (tcin, location_id)
        ORDER BY s.units_sold DESC NULLS LAST
        LIMIT 1000
    """
    try:
        cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )
    return make_table_response(
        rows=_rows_to_dicts(cols, rows),
        columns=cols,
        title="Sell-through and weeks-of-supply",
        extra={
            "sales_table": sales_table,
            "inv_table": inv_table,
            "sql": sql,
        },
        fmt=params.response_format,
    )


# --------------------------------------------------------------------------------------
# S&OP analytics tools (May 2026 patch)
# --------------------------------------------------------------------------------------
#
# These three tools all share a common shape: the data warehouse schema is discovered
# at runtime (not hardcoded), so each helper below first probes `warehouse.describe()`
# to find the right column for date/qty/location/etc, then composes SQL dynamically.
# If the required dataset hasn't been loaded yet, the tool returns DATA_UNAVAILABLE
# rather than a cryptic Catalog Error.


def _table_cols(warehouse: Warehouse, table: str) -> set[str] | None:
    """Set of column names in `table`, or None if the table doesn't exist."""
    desc = warehouse.describe()["tables"]
    if table not in desc:
        return None
    return {c["name"] for c in desc[table]["columns"]}


def _first_present(candidates: tuple[str, ...], cols: set[str]) -> str | None:
    return next((c for c in candidates if c in cols), None)


# Candidate columns shared across the new datasets. Order matters — first hit wins.
_DATE_COL_CANDIDATES = (
    "order_date",
    "po_date",
    "plan_date",
    "expected_date",
    "period_start_date",
    "period_end_date",
    "forecast_date",
    "week_end_date",
    "week_start_date",
    "sale_date",
    "snapshot_date",
)
_LOC_COL_CANDIDATES = (
    "location_id",
    "store_id",
    "loc_id",
    "store_nbr",
    "location_nbr",
)
_QTY_COL_CANDIDATES = (
    # In priority order: "remaining/open" first because for open-orders we want
    # what's *outstanding*, not the total ordered.
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
    # Then planned / expected.
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
    # Generic fallbacks.
    "units",
    "qty",
)
_STATUS_COL_CANDIDATES = (
    "order_status",
    "status",
    "fulfillment_status",
    "ship_status",
    "po_status",
)


def _in_list_sql(col: str, values: list[int] | None) -> str | None:
    """Build a safe `col IN (1,2,3)` clause, or None if values is empty/missing."""
    if not values:
        return None
    safe = ",".join(str(int(v)) for v in values)
    return f"{quote_ident(col)} IN ({safe})"


# ---------- bpd_get_open_orders ----------


async def get_open_orders(
    warehouse: Warehouse, params: OpenOrdersInput
) -> ToolResponse:
    """Outstanding Target POs to the vendor, summed by SKU.

    "Open" is heuristic: we prefer a remaining/outstanding quantity column if present;
    failing that, we exclude rows whose status looks fulfilled; failing both, we sum
    all ordered units placed on or before as_of_date. The choice is reported in `extra`.
    """
    cols = _table_cols(warehouse, "orders_daily")
    if cols is None:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="orders_daily not loaded yet — run bpd_sync_new_files first.",
            fmt=params.response_format,
        )

    date_col = _first_present(_DATE_COL_CANDIDATES, cols)
    qty_col = _first_present(_QTY_COL_CANDIDATES, cols)
    status_col = _first_present(_STATUS_COL_CANDIDATES, cols)
    loc_col = _first_present(_LOC_COL_CANDIDATES, cols)

    if qty_col is None or "tcin" not in cols:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "orders_daily missing required columns: need a qty/units column "
                f"(looked for {_QTY_COL_CANDIDATES}) and `tcin`. Actual columns: "
                f"{sorted(cols)}"
            ),
            fmt=params.response_format,
        )

    as_of = params.as_of_date or date.today()
    where_clauses: list[str] = []
    method: str

    if status_col:
        # Exclude statuses that look fulfilled/cancelled. Case-insensitive contains.
        where_clauses.append(
            f"COALESCE(UPPER({quote_ident(status_col)}), '') NOT IN "
            f"('FULFILLED','SHIPPED','CLOSED','CANCELLED','CANCELED','COMPLETE','RECEIVED','DELIVERED')"
        )
        method = f"status-filter (excludes {status_col} ∈ fulfilled/closed)"
    elif qty_col in (
        "open_units", "units_open", "units_remaining", "remaining_units",
        "qty_open", "open_qty", "qty_remaining", "remaining_qty",
        "outstanding_units", "outstanding_qty",
    ):
        # The column itself already represents outstanding; no extra filter.
        method = f"qty column ({qty_col}) already represents outstanding units"
    else:
        method = (
            f"no status/remaining column found; summing total ordered ({qty_col})"
        )

    if date_col:
        where_clauses.append(
            f"{quote_ident(date_col)} <= DATE '{as_of.isoformat()}'"
        )

    loc_filter = (
        _in_list_sql(loc_col, params.location_filter) if loc_col else None
    )
    if loc_filter:
        where_clauses.append(loc_filter)
    elif params.location_filter:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "location_filter supplied but orders_daily has no location column "
                f"(looked for {_LOC_COL_CANDIDATES})"
            ),
            fmt=params.response_format,
        )

    tcin_filter = _in_list_sql("tcin", params.tcin_filter)
    if tcin_filter:
        where_clauses.append(tcin_filter)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = (
        f"SELECT tcin, SUM({quote_ident(qty_col)}) AS open_units, "
        f"COUNT(*) AS line_count "
        f"FROM orders_daily {where_sql} "
        "GROUP BY tcin ORDER BY open_units DESC NULLS LAST"
    )

    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )

    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=f"Open orders as of {as_of.isoformat()}",
        extra={
            "method": method,
            "date_col": date_col,
            "qty_col": qty_col,
            "status_col": status_col,
            "location_col": loc_col,
            "sql": sql,
        },
        fmt=params.response_format,
    )


# ---------- bpd_get_upcoming_pos ----------


async def get_upcoming_pos(
    warehouse: Warehouse, params: UpcomingPosInput
) -> ToolResponse:
    """Forward-looking PO plan from po_plan_daily + po_plan_biweekly, grouped by week.

    If both tables exist, we UNION ALL them after projecting each to (tcin, week, qty).
    Each table's date column and qty column are resolved at runtime — the brief said
    not to hardcode them, and we don't.
    """
    daily_cols = _table_cols(warehouse, "po_plan_daily")
    biweekly_cols = _table_cols(warehouse, "po_plan_biweekly")
    if daily_cols is None and biweekly_cols is None:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="Neither po_plan_daily nor po_plan_biweekly is loaded yet.",
            fmt=params.response_format,
        )

    def _project(table: str, cols: set[str]) -> tuple[str, dict[str, str | None]] | None:
        date_col = _first_present(_DATE_COL_CANDIDATES, cols)
        qty_col = _first_present(_QTY_COL_CANDIDATES, cols)
        if date_col is None or qty_col is None or "tcin" not in cols:
            return None
        tcin_filter = _in_list_sql("tcin", params.tcin_filter)
        where = [
            f"{quote_ident(date_col)} >= current_date",
            f"{quote_ident(date_col)} < current_date + INTERVAL '{int(params.weeks_forward)} weeks'",
        ]
        if tcin_filter:
            where.append(tcin_filter)
        proj = (
            f"SELECT tcin, "
            f"date_trunc('week', {quote_ident(date_col)}) AS week, "
            f"{quote_ident(qty_col)} AS qty, "
            f"'{table}' AS source "
            f"FROM {quote_ident(table)} "
            f"WHERE {' AND '.join(where)}"
        )
        return proj, {"date_col": date_col, "qty_col": qty_col}

    projections = []
    resolved_cols: dict[str, dict[str, str | None]] = {}
    if daily_cols is not None:
        result = _project("po_plan_daily", daily_cols)
        if result is not None:
            projections.append(result[0])
            resolved_cols["po_plan_daily"] = result[1]
    if biweekly_cols is not None:
        result = _project("po_plan_biweekly", biweekly_cols)
        if result is not None:
            projections.append(result[0])
            resolved_cols["po_plan_biweekly"] = result[1]

    if not projections:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "po_plan_* tables exist but lack the columns we expect "
                "(need a date column and a qty/units column plus `tcin`)."
            ),
            fmt=params.response_format,
        )

    union_sql = " UNION ALL ".join(f"({p})" for p in projections)
    sql = (
        f"WITH planned AS ({union_sql}) "
        "SELECT tcin, week, SUM(qty) AS planned_units, "
        "string_agg(DISTINCT source, ',') AS sources "
        "FROM planned GROUP BY tcin, week ORDER BY week, tcin"
    )
    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=f"Upcoming POs (next {params.weeks_forward} weeks)",
        extra={"resolved_columns": resolved_cols, "sql": sql},
        fmt=params.response_format,
    )


# ---------- bpd_get_forecast_vs_actual ----------


async def get_forecast_vs_actual(
    warehouse: Warehouse, params: ForecastVsActualInput
) -> ToolResponse:
    """Join Target's DFE weekly forecast with sales_weekly actuals.

    Computes absolute and percentage variance per group, where the group is one of:
      * `by_sku_week` (default): tcin × week_end_date
      * `by_sku_location_week`: tcin × location × week_end_date
      * `by_sku`: tcin only (collapses time — useful for trailing-N-weeks totals)
    """
    fc_cols = _table_cols(warehouse, "forecast_weekly")
    act_cols = _table_cols(warehouse, "sales_weekly")
    if fc_cols is None or act_cols is None:
        missing = [
            t for t, c in (("forecast_weekly", fc_cols), ("sales_weekly", act_cols)) if c is None
        ]
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message=f"Required dataset(s) not loaded yet: {missing}",
            fmt=params.response_format,
        )

    fc_date = _first_present(("week_end_date", "forecast_date", "week_start_date"), fc_cols)
    act_date = _first_present(("week_end_date", "sale_date"), act_cols)
    fc_units = _first_present(
        ("forecast_units", "forecasted_units", "planned_units", "units", "qty"),
        fc_cols,
    )
    act_units = _first_present(
        ("units_sold", "units", "unit_count", "sales_units"), act_cols
    )
    fc_loc = _first_present(_LOC_COL_CANDIDATES, fc_cols)
    act_loc = _first_present(_LOC_COL_CANDIDATES, act_cols)

    missing_fields = [
        n
        for n, v in (
            ("forecast date col", fc_date),
            ("actual date col", act_date),
            ("forecast units col", fc_units),
            ("actual units col", act_units),
        )
        if v is None
    ]
    if missing_fields:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "Cannot align forecast and actual schemas: "
                f"missing {missing_fields}. "
                f"forecast cols: {sorted(fc_cols)}; "
                f"sales cols: {sorted(act_cols)}"
            ),
            fmt=params.response_format,
        )

    # WHERE clauses on each side.
    weeks_back = int(params.weeks_back)
    fc_where: list[str] = [
        f"{quote_ident(fc_date)} >= current_date - INTERVAL '{weeks_back} weeks'",
        f"{quote_ident(fc_date)} <= current_date",
    ]
    act_where: list[str] = [
        f"{quote_ident(act_date)} >= current_date - INTERVAL '{weeks_back} weeks'",
        f"{quote_ident(act_date)} <= current_date",
    ]
    tcin_filter = _in_list_sql("tcin", params.tcin_filter)
    if tcin_filter:
        fc_where.append(tcin_filter)
        act_where.append(tcin_filter)
    if params.location_filter:
        if fc_loc is None or act_loc is None:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message=(
                    "location_filter supplied but one of the tables lacks a "
                    f"location column (forecast loc={fc_loc}, sales loc={act_loc})"
                ),
                fmt=params.response_format,
            )
        fc_where.append(_in_list_sql(fc_loc, params.location_filter) or "TRUE")
        act_where.append(_in_list_sql(act_loc, params.location_filter) or "TRUE")

    # Build the projection / GROUP BY based on aggregate mode.
    if params.aggregate == "by_sku":
        group_cols = ("tcin",)
        select_join_key = "tcin"
    elif params.aggregate == "by_sku_location_week":
        if fc_loc is None or act_loc is None:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message="by_sku_location_week requires a location column on both tables.",
                fmt=params.response_format,
            )
        group_cols = ("tcin", "location_id", "week_end_date")
        select_join_key = "tcin, location_id, week_end_date"
    else:  # by_sku_week (default)
        group_cols = ("tcin", "week_end_date")
        select_join_key = "tcin, week_end_date"

    fc_loc_proj = (
        f"{quote_ident(fc_loc)} AS location_id, " if (fc_loc and "location_id" in group_cols) else ""
    )
    act_loc_proj = (
        f"{quote_ident(act_loc)} AS location_id, " if (act_loc and "location_id" in group_cols) else ""
    )
    fc_week_proj = (
        f"{quote_ident(fc_date)} AS week_end_date, " if "week_end_date" in group_cols else ""
    )
    act_week_proj = (
        f"{quote_ident(act_date)} AS week_end_date, " if "week_end_date" in group_cols else ""
    )

    sql = f"""
        WITH fc AS (
            SELECT tcin, {fc_loc_proj}{fc_week_proj}
                   SUM({quote_ident(fc_units)}) AS forecast_units
            FROM forecast_weekly
            WHERE {' AND '.join(fc_where)}
            GROUP BY {select_join_key}
        ),
        act AS (
            SELECT tcin, {act_loc_proj}{act_week_proj}
                   SUM({quote_ident(act_units)}) AS actual_units
            FROM sales_weekly
            WHERE {' AND '.join(act_where)}
            GROUP BY {select_join_key}
        )
        SELECT {select_join_key},
               COALESCE(fc.forecast_units, 0) AS forecast_units,
               COALESCE(act.actual_units, 0) AS actual_units,
               (COALESCE(act.actual_units, 0) - COALESCE(fc.forecast_units, 0)) AS variance_units,
               CASE WHEN COALESCE(fc.forecast_units, 0) = 0 THEN NULL
                    ELSE (COALESCE(act.actual_units, 0) - fc.forecast_units) * 1.0
                         / fc.forecast_units
               END AS variance_pct
        FROM fc FULL OUTER JOIN act USING ({select_join_key})
        ORDER BY {select_join_key}
        LIMIT 2000
    """
    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=params.response_format,
        )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=(
            f"Forecast vs actual (trailing {weeks_back} weeks, "
            f"aggregate={params.aggregate})"
        ),
        extra={
            "forecast_date_col": fc_date,
            "actual_date_col": act_date,
            "forecast_units_col": fc_units,
            "actual_units_col": act_units,
            "sql": sql,
        },
        fmt=params.response_format,
    )
