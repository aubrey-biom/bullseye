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
# S&OP analytics tools (May 2026 patch; rebuilt on the central registry in Patch #10)
# --------------------------------------------------------------------------------------
#
# These tools resolve columns through column_roles.resolve_column at call time —
# the SAME registry the other analytics tools use. The module-local candidate
# tuples that used to live here (a duplicated, divergent resolver full of
# invented names) were deleted in Patch #10: registry fixes were invisible to
# these tools, which is exactly how they shipped hard-broken against real
# Target orders/po_plan columns.


def _try_resolve(
    warehouse: Warehouse, dataset: str, role: str
) -> ResolvedColumn | None:
    """`resolve_column`, but None instead of raising — for optional roles."""
    try:
        return resolve_column(warehouse, dataset, role)
    except ColumnNotFound:
        return None


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
    """Outstanding Target POs summed by SKU, derived from the latest-state order book.

    orders_daily is a delta feed materialized as LATEST STATE: its natural key
    (purchase_order_id, tcin, receiving_location_id) has no date column and each
    load replaces matching keys, so the table always holds exactly one row —
    the last-known state — per PO line. No snapshot filter or dedup is needed.

    There is no physical "open units" column (and `purchase_order_active_f` is
    unpopulated at source), so open units are DERIVED per line as
    ordered - received - cancel_remaining, keeping lines where that is > 0
    (Patch #10).
    """
    table = "orders_daily"
    fmt = params.response_format
    if not table_exists(warehouse, table):
        return _missing_table_error(table=table, fmt=fmt)

    try:
        ordered = resolve_column(warehouse, table, "ordered")
        received = resolve_column(warehouse, table, "received")
        cancel_rem = resolve_column(warehouse, table, "cancel_remaining")
        po_id = resolve_column(warehouse, table, "po_id")
        tcin_col = resolve_column(warehouse, table, "tcin")
        loc_col = resolve_column(warehouse, table, "location")
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)
    created = _try_resolve(warehouse, table, "order_created")

    where_clauses: list[str] = []
    scope = "whole order book (latest-known state of every PO line)"
    if params.as_of_date is not None:
        if created is None:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message=(
                    "as_of_date filtering needs the PO-creation date column "
                    "(role 'order_created', e.g. purchase_order_create_d), "
                    f"which {table} does not have."
                ),
                fmt=fmt,
            )
        where_clauses.append(
            f"{created.select_as_date()} <= DATE '{params.as_of_date.isoformat()}'"
        )
        scope = (
            f"POs created on or before {params.as_of_date.isoformat()} — each "
            "still reflects its latest-known state, NOT a reconstruction of the "
            "book as it stood on that date (the table is latest-state only)"
        )

    loc_filter = _in_list_sql(loc_col.name, params.location_filter)
    if loc_filter:
        where_clauses.append(loc_filter)
    tcin_filter = _in_list_sql(tcin_col.name, params.tcin_filter)
    if tcin_filter:
        where_clauses.append(tcin_filter)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    open_expr = (
        f"COALESCE({quote_ident(ordered.name)}, 0) "
        f"- COALESCE({quote_ident(received.name)}, 0) "
        f"- COALESCE({quote_ident(cancel_rem.name)}, 0)"
    )
    sql = (
        f"WITH lines AS ("
        f"SELECT {quote_ident(tcin_col.name)} AS tcin, "
        f"{quote_ident(po_id.name)} AS po_id, "
        f"{open_expr} AS open_units "
        f"FROM {quote_ident(table)} {where_sql}"
        ") "
        "SELECT tcin, COUNT(DISTINCT po_id) AS po_count, "
        "SUM(open_units) AS open_units, COUNT(*) AS line_count "
        "FROM lines WHERE open_units > 0 "
        "GROUP BY tcin ORDER BY open_units DESC NULLS LAST"
    )

    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=fmt,
        )

    title = (
        "Open orders (latest order-book state)"
        if params.as_of_date is None
        else f"Open orders — POs created on or before {params.as_of_date.isoformat()}"
    )
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=title,
        extra={
            "method": (
                f"derived: {ordered.name} - COALESCE({received.name},0) - "
                f"COALESCE({cancel_rem.name},0) per PO line, keeping lines > 0"
            ),
            "scope": scope,
            "resolved_columns": {
                "ordered": ordered.name,
                "received": received.name,
                "cancel_remaining": cancel_rem.name,
                "po_id": po_id.name,
                "location": loc_col.name,
                "order_created": created.name if created else None,
            },
            "sql": sql,
        },
        fmt=fmt,
    )


# ---------- bpd_get_upcoming_pos ----------


async def get_upcoming_pos(
    warehouse: Warehouse, params: UpcomingPosInput
) -> ToolResponse:
    """Forward-looking PO plan from po_plan_daily + po_plan_biweekly.

    Both po_plan tables ACCUMULATE snapshots: their natural key includes
    `business_d` (the as-of date), so every generation of the full plan
    coexists in the table. Each table is therefore filtered to its own latest
    `business_d` before anything is summed — without that filter, totals
    multiply by the number of snapshots retained (Patch #10).

    The forward window is on `order_d` (the date each planned PO is targeted
    at). Results are grouped by (tcin, week, source): the daily and biweekly
    plans have different horizons and must not be silently added together for
    the same planned order. Per-source totals are in `extra.source_totals`.
    """
    fmt = params.response_format
    tables = [
        t for t in ("po_plan_daily", "po_plan_biweekly") if table_exists(warehouse, t)
    ]
    if not tables:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message="Neither po_plan_daily nor po_plan_biweekly is loaded yet.",
            fmt=fmt,
        )

    projections: list[str] = []
    resolved_cols: dict[str, dict[str, Any]] = {}
    skipped_tables: dict[str, str] = {}
    empty_tables: list[str] = []
    for table in tables:
        try:
            snap = resolve_column(warehouse, table, "date")  # business_d (as-of)
            order_d = resolve_column(warehouse, table, "order_date")  # order_d
            qty = resolve_column(warehouse, table, "units")  # ordered_q
            tcin_col = resolve_column(warehouse, table, "tcin")
        except ColumnNotFound as e:
            skipped_tables[table] = str(e)
            continue

        # Normalize the snapshot column to a DATE regardless of physical type
        # (VARCHAR ISO string, DATE, or TIMESTAMP). Wrapped so one table's bad
        # date value degrades to skipped_tables instead of aborting the tool
        # (review fix).
        snap_day = f"CAST({snap.select_as_date()} AS DATE)"
        try:
            _, mx = warehouse.execute_sql(
                f"SELECT MAX({snap_day}) FROM {quote_ident(table)}"
            )
        except Exception as e:
            skipped_tables[table] = (
                f"latest-snapshot probe failed: {type(e).__name__}: {e}"
            )
            continue
        latest_snapshot = mx[0][0] if mx else None
        if latest_snapshot is None:
            # Present-but-empty is a data-availability state, not a schema
            # problem — tracked separately so it never reads as breakage
            # (review fix: the health smoke test treats DATA_UNAVAILABLE as
            # a benign skip).
            empty_tables.append(table)
            continue
        latest_iso = str(latest_snapshot)[:10]

        order_expr = order_d.select_as_date()
        where = [
            f"{snap_day} = DATE '{latest_iso}'",
            f"{order_expr} >= current_date",
            f"{order_expr} < current_date + INTERVAL '{int(params.weeks_forward)} weeks'",
        ]
        tcin_filter = _in_list_sql(tcin_col.name, params.tcin_filter)
        if tcin_filter:
            where.append(tcin_filter)
        projections.append(
            f"SELECT {quote_ident(tcin_col.name)} AS tcin, "
            f"date_trunc('week', {order_expr}) AS week, "
            f"{quote_ident(qty.name)} AS qty, "
            f"'{table}' AS source "
            f"FROM {quote_ident(table)} "
            f"WHERE {' AND '.join(where)}"
        )
        resolved_cols[table] = {
            "snapshot_col": snap.name,
            "order_date_col": order_d.name,
            "qty_col": qty.name,
            "latest_snapshot": latest_iso,
        }

    if not projections:
        if skipped_tables:
            return make_error_response(
                code="SCHEMA_INCOMPATIBLE",
                message=(
                    "po_plan tables exist but no projection could be built — "
                    + "; ".join(f"{t}: {msg}" for t, msg in skipped_tables.items())
                    + (f"; empty: {empty_tables}" if empty_tables else "")
                ),
                details={**skipped_tables, "empty_tables": empty_tables},
                fmt=fmt,
            )
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message=(
                f"po_plan table(s) {empty_tables} exist but contain no rows yet "
                "— run bpd_sync_new_files (or the feed may have nothing planned)."
            ),
            details={"empty_tables": empty_tables},
            fmt=fmt,
        )

    union_sql = " UNION ALL ".join(f"({p})" for p in projections)
    sql = (
        f"WITH planned AS ({union_sql}) "
        "SELECT tcin, week, source, SUM(qty) AS planned_units, "
        "COUNT(*) AS line_count "
        "FROM planned GROUP BY tcin, week, source ORDER BY week, tcin, source"
    )
    try:
        out_cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=fmt,
        )

    dict_rows = _rows_to_dicts(out_cols, rows)
    source_totals: dict[str, Any] = {}
    for r in dict_rows:
        source_totals[r["source"]] = (
            source_totals.get(r["source"], 0) + (r["planned_units"] or 0)
        )
    extra: dict[str, Any] = {
        "resolved_columns": resolved_cols,
        "source_totals": source_totals,
        "sql": sql,
    }
    if skipped_tables:
        extra["skipped_tables"] = skipped_tables
    if empty_tables:
        extra["empty_tables"] = empty_tables
    return make_table_response(
        rows=dict_rows,
        columns=out_cols,
        title=(
            f"Upcoming POs (next {params.weeks_forward} weeks, "
            "latest snapshot per source)"
        ),
        extra=extra,
        fmt=fmt,
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

    # Patch #5: forecast_weekly uses Sunday-anchored fiscal_week_begin_d while
    # sales_weekly uses Saturday-anchored sales_date. Pre-fix, the FULL OUTER
    # JOIN on week_end_date produced ZERO matches because the two sides were
    # always 6 days apart. Canonicalize both to week-end (Saturday) when
    # joining: if the resolved forecast date column is a week-BEGIN field,
    # shift +6 days. Otherwise leave it alone (some Target variants ship a
    # week-end column directly).
    fc_name_lower = fc_date.name.lower()
    fc_is_week_begin = "begin" in fc_name_lower or "start" in fc_name_lower
    if fc_is_week_begin:
        # DuckDB's DATE + INTERVAL returns TIMESTAMP; cast back to DATE so the
        # join column type matches sales_weekly's pure DATE.
        fc_week_end_expr = f"CAST({fc_date_expr} + INTERVAL 6 DAY AS DATE)"
    else:
        fc_week_end_expr = fc_date_expr

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
    # Project the canonical Saturday week-end on BOTH sides so the join works.
    fc_week_proj = (
        f"{fc_week_end_expr} AS week_end_date, " if "week_end_date" in group_cols else ""
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
            "forecast_week_anchor": "begin" if fc_is_week_begin else "end",
            "forecast_week_shift_days": 6 if fc_is_week_begin else 0,
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
