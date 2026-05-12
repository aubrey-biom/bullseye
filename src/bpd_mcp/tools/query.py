"""Query tools: run_sql, sales_summary, top_skus, inventory_snapshot, sell_through,
describe_schema, plus the S&OP analytics added in the May 2026 patch
(open_orders, upcoming_pos, forecast_vs_actual)."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..column_roles import (
    ColumnNotFound,
    ResolvedColumn,
    resolve_column,
    table_exists,
)
from ..formatting import (
    make_error_response,
    make_kv_response,
    make_table_response,
)
from ..schemas import (
    DescribeSchemaInput,
    ExportQueryToCsvInput,
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

# --------------------------------------------------------------------------------------
# Column-resolution helpers (Patch #4)
# --------------------------------------------------------------------------------------
#
# All schema introspection happens at *call time* (not at module load) so a sync
# that creates a new table is visible without restarting the MCP. See Issue 6.


def _missing_table_error(
    *, table: str, fmt: str, hint: str | None = None
) -> ToolResponse:
    return make_error_response(
        code="DATA_UNAVAILABLE",
        message=(
            f"dataset table {table!r} not loaded yet — run bpd_sync_new_files first"
            + (f". {hint}" if hint else "")
        ),
        details={"dataset": table},
        fmt=fmt,
    )


def _column_not_found_error(err: ColumnNotFound, *, fmt: str) -> ToolResponse:
    """Convert a ColumnNotFound into a diagnostic-rich tool error.

    The brief specifically asked that the error detail include the dataset,
    role, candidates tried, and the actual columns present — so Claude (or
    the user) can immediately see "the table has X but my candidate list
    only had Y" and add the alias.
    """
    return make_error_response(
        code="SCHEMA_INCOMPATIBLE",
        message=(
            f"role {err.detail['role']!r} could not be resolved for dataset "
            f"{err.detail['dataset']!r}; tried {err.detail['candidates']}; "
            f"table actually has {err.detail['actual_columns']}"
        ),
        details=err.detail,
        fmt=fmt,
    )


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


def _pick_sales_table(warehouse: Warehouse, grain: str) -> str | None:
    """Pick the sales table that matches the grain. Fresh introspection per call."""
    desired = "sales_daily" if grain == "day" else "sales_weekly"
    if table_exists(warehouse, desired):
        return desired
    for fallback in ("sales_weekly", "sales_daily"):
        if table_exists(warehouse, fallback):
            return fallback
    return None


async def get_sales_summary(
    warehouse: Warehouse, params: SalesSummaryInput
) -> ToolResponse:
    fmt = params.response_format
    table = _pick_sales_table(warehouse, params.grain)
    if table is None:
        return _missing_table_error(
            table="sales_daily/sales_weekly",
            fmt=fmt,
            hint="No sales table present yet.",
        )

    try:
        date_col = resolve_column(warehouse, table, "date")
        units_col = resolve_column(warehouse, table, "units")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)
    # Dollars is optional — silently fall back to summing only units if absent.
    try:
        dollars_col: ResolvedColumn | None = resolve_column(warehouse, table, "dollars")
    except ColumnNotFound:
        dollars_col = None

    date_expr = date_col.select_as_date()  # casts VARCHAR → DATE if needed
    if params.grain == "day":
        bucket = date_expr
    elif params.grain == "week":
        bucket = f"date_trunc('week', {date_expr})"
    else:
        bucket = f"date_trunc('month', {date_expr})"

    where_clauses: list[str] = []
    if params.start_date:
        where_clauses.append(f"{date_expr} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_clauses.append(f"{date_expr} <= DATE '{params.end_date.isoformat()}'")
    if params.tcin is not None:
        try:
            tcin_col = resolve_column(warehouse, table, "tcin")
            where_clauses.append(
                f"{quote_ident(tcin_col.name)} = {int(params.tcin)}"
            )
        except ColumnNotFound:
            where_clauses.append(f"tcin = {int(params.tcin)}")
    if params.location_id is not None:
        try:
            loc_col = resolve_column(warehouse, table, "location")
            where_clauses.append(
                f"{quote_ident(loc_col.name)} = {int(params.location_id)}"
            )
        except ColumnNotFound:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message=f"location_id filter requested but no location column on {table}",
                fmt=fmt,
            )

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if dollars_col is None:
        sql = (
            f"SELECT {bucket} AS bucket, "
            f"SUM({quote_ident(units_col.name)}) AS total_units "
            f"FROM {quote_ident(table)} {where_sql} "
            "GROUP BY bucket ORDER BY bucket"
        )
    else:
        sql = (
            f"SELECT {bucket} AS bucket, "
            f"SUM({quote_ident(units_col.name)}) AS total_units, "
            f"SUM({quote_ident(dollars_col.name)}) AS total_dollars "
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
            fmt=fmt,
        )

    dict_rows = _rows_to_dicts(cols, rows)
    return make_table_response(
        rows=dict_rows,
        columns=cols,
        title=f"Sales summary ({params.grain}, table={table})",
        extra={
            "table": table,
            "date_col": date_col.name,
            "date_col_type": date_col.duckdb_type,
            "units_col": units_col.name,
            "dollars_col": dollars_col.name if dollars_col else None,
            "sql": sql,
        },
        fmt=fmt,
    )


# ---------- bpd_get_top_skus ----------


async def get_top_skus(warehouse: Warehouse, params: TopSkusInput) -> ToolResponse:
    fmt = params.response_format
    table = _pick_sales_table(warehouse, "week")
    if table is None:
        return _missing_table_error(table="sales_weekly", fmt=fmt)

    try:
        date_col = resolve_column(warehouse, table, "date")
        tcin_col = resolve_column(warehouse, table, "tcin")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    metric_role = "dollars" if params.by == "dollars" else "units"
    try:
        metric_col = resolve_column(warehouse, table, metric_role)
    except ColumnNotFound as e:
        # If the user asked for "dollars" and there's no dollar column on this
        # table, fall back to units rather than failing — give them *something*.
        if metric_role == "dollars":
            try:
                metric_col = resolve_column(warehouse, table, "units")
            except ColumnNotFound:
                return _column_not_found_error(e, fmt=fmt)
        else:
            return _column_not_found_error(e, fmt=fmt)

    date_expr = date_col.select_as_date()
    where_clauses: list[str] = []
    if params.start_date:
        where_clauses.append(f"{date_expr} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_clauses.append(f"{date_expr} <= DATE '{params.end_date.isoformat()}'")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = (
        f"SELECT {quote_ident(tcin_col.name)} AS tcin, "
        f"SUM({quote_ident(metric_col.name)}) AS metric_total "
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
            fmt=fmt,
        )
    dict_rows = _rows_to_dicts(cols, rows)
    return make_table_response(
        rows=dict_rows,
        columns=cols,
        title=f"Top {params.top_n} SKUs by {params.by}",
        extra={
            "table": table,
            "metric_col": metric_col.name,
            "metric_role": metric_role,
            "sql": sql,
        },
        fmt=fmt,
    )


# ---------- bpd_get_inventory_snapshot ----------


def _pick_inventory_table(warehouse: Warehouse) -> str | None:
    for table in ("inventory_daily", "inventory_weekly"):
        if table_exists(warehouse, table):
            return table
    return None


async def get_inventory_snapshot(
    warehouse: Warehouse, params: InventorySnapshotInput
) -> ToolResponse:
    fmt = params.response_format
    table = _pick_inventory_table(warehouse)
    if table is None:
        return _missing_table_error(table="inventory_daily/inventory_weekly", fmt=fmt)

    try:
        date_col = resolve_column(warehouse, table, "date")
        on_hand_col = resolve_column(warehouse, table, "on_hand")
        tcin_col = resolve_column(warehouse, table, "tcin")
        loc_col = resolve_column(warehouse, table, "location")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    as_of = params.as_of or date.today()
    date_expr = date_col.select_as_date()
    where: list[str] = [f"{date_expr} <= DATE '{as_of.isoformat()}'"]
    if params.tcin is not None:
        where.append(f"{quote_ident(tcin_col.name)} = {int(params.tcin)}")
    if params.location_id is not None:
        where.append(f"{quote_ident(loc_col.name)} = {int(params.location_id)}")

    sql = f"""
        WITH ranked AS (
            SELECT {quote_ident(tcin_col.name)} AS tcin,
                   {quote_ident(loc_col.name)} AS location_id,
                   {date_expr} AS dt,
                   {quote_ident(on_hand_col.name)} AS on_hand,
                   ROW_NUMBER() OVER (
                       PARTITION BY {quote_ident(tcin_col.name)}, {quote_ident(loc_col.name)}
                       ORDER BY {date_expr} DESC
                   ) AS rn
            FROM {quote_ident(table)}
            WHERE {' AND '.join(where)}
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
            fmt=fmt,
        )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=f"Inventory snapshot as of {as_of.isoformat()} (table={table})",
        extra={
            "table": table,
            "date_col": date_col.name,
            "date_col_type": date_col.duckdb_type,
            "on_hand_col": on_hand_col.name,
        },
        fmt=fmt,
    )


# ---------- bpd_get_sell_through ----------


async def get_sell_through(warehouse: Warehouse, params: SellThroughInput) -> ToolResponse:
    fmt = params.response_format
    sales_table = _pick_sales_table(warehouse, "week")
    inv_table = _pick_inventory_table(warehouse)
    if sales_table is None or inv_table is None:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="Need both a sales_weekly-ish table and an inventory table loaded.",
            details={
                "sales_table_present": sales_table is not None,
                "inventory_table_present": inv_table is not None,
            },
            fmt=fmt,
        )
    try:
        sales_date = resolve_column(warehouse, sales_table, "date")
        sales_units = resolve_column(warehouse, sales_table, "units")
        sales_tcin = resolve_column(warehouse, sales_table, "tcin")
        sales_loc = resolve_column(warehouse, sales_table, "location")
        inv_date = resolve_column(warehouse, inv_table, "date")
        inv_on_hand = resolve_column(warehouse, inv_table, "on_hand")
        inv_tcin = resolve_column(warehouse, inv_table, "tcin")
        inv_loc = resolve_column(warehouse, inv_table, "location")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    sales_date_expr = sales_date.select_as_date()
    inv_date_expr = inv_date.select_as_date()

    where_sales: list[str] = []
    if params.start_date:
        where_sales.append(f"{sales_date_expr} >= DATE '{params.start_date.isoformat()}'")
    if params.end_date:
        where_sales.append(f"{sales_date_expr} <= DATE '{params.end_date.isoformat()}'")
    if params.tcin is not None:
        where_sales.append(f"{quote_ident(sales_tcin.name)} = {int(params.tcin)}")
    if params.location_id is not None:
        where_sales.append(f"{quote_ident(sales_loc.name)} = {int(params.location_id)}")
    where_sales_sql = ("WHERE " + " AND ".join(where_sales)) if where_sales else ""

    sql = f"""
        WITH s AS (
            SELECT {quote_ident(sales_tcin.name)} AS tcin,
                   {quote_ident(sales_loc.name)} AS location_id,
                   SUM({quote_ident(sales_units.name)}) AS units_sold,
                   COUNT(DISTINCT {sales_date_expr}) AS weeks_observed
            FROM {quote_ident(sales_table)}
            {where_sales_sql}
            GROUP BY tcin, location_id
        ),
        latest_inv AS (
            SELECT tcin, location_id, on_hand
            FROM (
                SELECT {quote_ident(inv_tcin.name)} AS tcin,
                       {quote_ident(inv_loc.name)} AS location_id,
                       {quote_ident(inv_on_hand.name)} AS on_hand,
                       ROW_NUMBER() OVER (
                           PARTITION BY {quote_ident(inv_tcin.name)}, {quote_ident(inv_loc.name)}
                           ORDER BY {inv_date_expr} DESC
                       ) AS rn
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
            fmt=fmt,
        )
    return make_table_response(
        rows=_rows_to_dicts(cols, rows),
        columns=cols,
        title="Sell-through and weeks-of-supply",
        extra={
            "sales_table": sales_table,
            "inv_table": inv_table,
            "resolved_columns": {
                "sales_date": sales_date.name,
                "sales_units": sales_units.name,
                "sales_location": sales_loc.name,
                "inv_date": inv_date.name,
                "inv_on_hand": inv_on_hand.name,
                "inv_location": inv_loc.name,
            },
            "sql": sql,
        },
        fmt=fmt,
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

    Pre-week vs post-hoc forecast: forecast_weekly contains multiple snapshots
    per (tcin, location, week). When `as_of_date` is omitted (default), we pick
    the latest forecast published before the week begins — Target's pre-week
    prediction. Set `as_of_date` explicitly to lock the cutoff.
    """
    fmt = params.response_format
    if not table_exists(warehouse, "forecast_weekly"):
        return _missing_table_error(table="forecast_weekly", fmt=fmt)
    if not table_exists(warehouse, "sales_weekly"):
        return _missing_table_error(table="sales_weekly", fmt=fmt)

    try:
        fc_date = resolve_column(warehouse, "forecast_weekly", "date")
        fc_units = resolve_column(warehouse, "forecast_weekly", "units")
        fc_tcin = resolve_column(warehouse, "forecast_weekly", "tcin")
        act_date = resolve_column(warehouse, "sales_weekly", "date")
        act_units = resolve_column(warehouse, "sales_weekly", "units")
        act_tcin = resolve_column(warehouse, "sales_weekly", "tcin")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)
    try:
        fc_snap = resolve_column(warehouse, "forecast_weekly", "snapshot_date")
    except ColumnNotFound:
        fc_snap = None
    try:
        fc_loc = resolve_column(warehouse, "forecast_weekly", "location")
    except ColumnNotFound:
        fc_loc = None
    try:
        act_loc = resolve_column(warehouse, "sales_weekly", "location")
    except ColumnNotFound:
        act_loc = None

    fc_date_expr = fc_date.select_as_date()
    act_date_expr = act_date.select_as_date()

    weeks_back = int(params.weeks_back)
    fc_where: list[str] = [
        f"{fc_date_expr} >= current_date - INTERVAL '{weeks_back} weeks'",
        f"{fc_date_expr} <= current_date",
    ]
    act_where: list[str] = [
        f"{act_date_expr} >= current_date - INTERVAL '{weeks_back} weeks'",
        f"{act_date_expr} <= current_date",
    ]
    if params.tcin_filter:
        fc_in = ",".join(str(int(v)) for v in params.tcin_filter)
        fc_where.append(f"{quote_ident(fc_tcin.name)} IN ({fc_in})")
        act_where.append(f"{quote_ident(act_tcin.name)} IN ({fc_in})")
    if params.location_filter:
        if fc_loc is None or act_loc is None:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message=(
                    "location_filter supplied but one of the tables lacks a "
                    f"location column (forecast loc={fc_loc and fc_loc.name}, "
                    f"sales loc={act_loc and act_loc.name})"
                ),
                fmt=fmt,
            )
        loc_in = ",".join(str(int(v)) for v in params.location_filter)
        fc_where.append(f"{quote_ident(fc_loc.name)} IN ({loc_in})")
        act_where.append(f"{quote_ident(act_loc.name)} IN ({loc_in})")

    # Snapshot disambiguation: forecast_weekly may have multiple snapshots
    # (last_update_d) per week. Pick the latest snapshot ≤ as_of_date so we
    # don't accidentally compare against Target's revised post-hoc forecast.
    snap_cte = ""
    if fc_snap is not None:
        snap_date_expr = fc_snap.select_as_date()
        # `as_of_date` defaults to "the day before each forecast week begins"
        # so we get the prediction Target actually published pre-week.
        if params.as_of_date is None:
            cutoff_expr = f"({fc_date_expr} - INTERVAL '1 day')"
        else:
            cutoff_expr = f"DATE '{params.as_of_date.isoformat()}'"
        partition_cols = [quote_ident(fc_tcin.name), fc_date_expr]
        if fc_loc is not None:
            partition_cols.insert(1, quote_ident(fc_loc.name))
        snap_cte = f"""
            ranked_fc AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY {", ".join(partition_cols)}
                           ORDER BY {snap_date_expr} DESC
                       ) AS _snap_rn
                FROM forecast_weekly
                WHERE {snap_date_expr} <= {cutoff_expr}
                  AND {' AND '.join(fc_where)}
            ),
            fc_src AS (SELECT * FROM ranked_fc WHERE _snap_rn = 1),
        """
        fc_from_clause = "fc_src"
        fc_where_clause = ""  # already applied inside ranked_fc
    else:
        fc_from_clause = "forecast_weekly"
        fc_where_clause = f"WHERE {' AND '.join(fc_where)}"

    # Build the projection / GROUP BY based on aggregate mode.
    if params.aggregate == "by_sku":
        group_cols = ("tcin",)
        select_join_key = "tcin"
    elif params.aggregate == "by_sku_location_week":
        if fc_loc is None or act_loc is None:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message="by_sku_location_week requires a location column on both tables.",
                fmt=fmt,
            )
        group_cols = ("tcin", "location_id", "week_end_date")
        select_join_key = "tcin, location_id, week_end_date"
    else:  # by_sku_week (default)
        group_cols = ("tcin", "week_end_date")
        select_join_key = "tcin, week_end_date"

    fc_loc_proj = (
        f"{quote_ident(fc_loc.name)} AS location_id, " if (fc_loc and "location_id" in group_cols) else ""
    )
    act_loc_proj = (
        f"{quote_ident(act_loc.name)} AS location_id, " if (act_loc and "location_id" in group_cols) else ""
    )
    fc_week_proj = (
        f"{fc_date_expr} AS week_end_date, " if "week_end_date" in group_cols else ""
    )
    act_week_proj = (
        f"{act_date_expr} AS week_end_date, " if "week_end_date" in group_cols else ""
    )

    sql = f"""
        WITH {snap_cte}
        fc AS (
            SELECT {quote_ident(fc_tcin.name)} AS tcin, {fc_loc_proj}{fc_week_proj}
                   SUM({quote_ident(fc_units.name)}) AS forecast_units
            FROM {fc_from_clause}
            {fc_where_clause}
            GROUP BY {select_join_key}
        ),
        act AS (
            SELECT {quote_ident(act_tcin.name)} AS tcin, {act_loc_proj}{act_week_proj}
                   SUM({quote_ident(act_units.name)}) AS actual_units
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
            fmt=fmt,
        )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=(
            f"Forecast vs actual (trailing {weeks_back} weeks, "
            f"aggregate={params.aggregate})"
        ),
        extra={
            "forecast_date_col": fc_date.name,
            "forecast_date_type": fc_date.duckdb_type,
            "forecast_units_col": fc_units.name,
            "forecast_snapshot_col": fc_snap.name if fc_snap else None,
            "actual_date_col": act_date.name,
            "actual_units_col": act_units.name,
            "as_of_date_used": (
                params.as_of_date.isoformat()
                if params.as_of_date
                else "pre-week (week_start - 1 day)"
            ),
            "sql": sql,
        },
        fmt=fmt,
    )


# --------------------------------------------------------------------------------------
# bpd_export_query_to_csv (Patch #4, Issue 5)
# --------------------------------------------------------------------------------------


def _validate_export_filename(name: str) -> str | None:
    """Return None if `name` is acceptable, else an error message describing why."""
    if "/" in name or "\\" in name:
        return f"filename {name!r} contains a path separator; only a bare filename is allowed"
    if name.startswith("."):
        return f"filename {name!r} may not start with a dot"
    if not name.lower().endswith(".csv"):
        return f"filename {name!r} must end in .csv"
    if any(ch in name for ch in ("\x00", "\n", "\r")):
        return f"filename {name!r} contains a control character"
    return None


async def export_query_to_csv(
    read_only_warehouse: Warehouse,
    settings,  # avoid circular import on Settings type
    params: ExportQueryToCsvInput,
) -> ToolResponse:
    """Run a read-only SQL query and write the result to ~/.bpd-mcp/exports/<filename>.

    Validation:
      * filename: no path separators, no leading dot, must end in `.csv`.
      * SQL: same engine-level + validator-level read-only enforcement as bpd_run_sql.
    """
    import csv
    from pathlib import Path as _Path

    fmt = params.response_format

    if not read_only_warehouse.read_only:
        return make_error_response(
            code="SQL_BLOCKED",
            message="bpd_export_query_to_csv requires the read-only view",
            fmt=fmt,
        )

    err = _validate_export_filename(params.filename)
    if err:
        return make_error_response(
            code="INVALID_FILENAME",
            message=err,
            details={"filename": params.filename},
            fmt=fmt,
        )

    try:
        cleaned = validate(params.sql)
    except SqlBlocked as e:
        return make_error_response(
            code="SQL_BLOCKED",
            message=str(e),
            details={"sql": params.sql[:500]},
            fmt=fmt,
        )

    wrapped = wrap_with_limit(cleaned, int(params.max_rows))
    try:
        read_only_warehouse.execute_sql(f"EXPLAIN {wrapped}")
    except Exception as e:
        return make_error_response(
            code="SQL_PLAN_FAILED",
            message=f"EXPLAIN failed: {e}",
            fmt=fmt,
        )
    try:
        out_cols, rows = read_only_warehouse.execute_sql(wrapped)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            fmt=fmt,
        )

    exports_dir = _Path(settings.data_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    target = exports_dir / params.filename

    with target.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if params.include_header:
            writer.writerow(out_cols)
        for row in rows:
            writer.writerow(row)

    import os
    os.chmod(target, 0o644)
    bytes_written = target.stat().st_size

    payload = {
        "path": str(target),
        "rows_written": len(rows),
        "columns": out_cols,
        "bytes_written": bytes_written,
    }
    return make_kv_response(
        data=payload,
        title=f"Exported {len(rows):,} row(s) to {target.name}",
        fmt=fmt,
    )
