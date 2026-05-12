"""Query tools: run_sql, sales_summary, top_skus, inventory_snapshot, sell_through, describe_schema."""

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
    InventorySnapshotInput,
    RunSqlInput,
    SalesSummaryInput,
    SellThroughInput,
    ToolResponse,
    TopSkusInput,
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
