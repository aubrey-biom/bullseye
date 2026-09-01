"""Query tools: run_sql, sales_summary, top_skus, inventory_snapshot, sell_through,
describe_schema, plus the S&OP analytics added in the May 2026 patch
(open_orders, upcoming_pos, forecast_vs_actual).

DIALECT: every statement built here is **BigQuery Standard SQL**. The DuckDB
spellings this module used to emit are gone; the ones that mattered, and why:

  * `"x"` is a STRING LITERAL in BigQuery, not an identifier. All quoting goes
    through `quote_ident`, which now emits backticks (see bq.quote_ident).
  * `TRY_CAST` -> `SAFE_CAST`. Not tidiness: bpd_raw ships several date columns
    as STRING holding Target's literal `""` placeholder, and a plain CAST is a
    hard 400 `Invalid date` — optimizer-dependently, so a passing smoke query
    proves nothing.
  * `date_trunc('week', x)` -> `DATE_TRUNC(x, WEEK(MONDAY))`. Argument order
    flips AND the default anchor differs; see the comment at the two call sites.
  * `x +/- INTERVAL n DAY` -> `DATE_ADD/DATE_SUB(x, INTERVAL n DAY)`. The infix
    form returns DATETIME in BigQuery, which leaks a time component into GROUP
    BY keys and into Python-side date arithmetic.
  * `current_date` -> `CURRENT_DATE()` (already UTC, matching the DuckDB session
    pin that used to be set explicitly).
  * `DATE - DATE` -> `DATE_DIFF(a, b, DAY)`. BigQuery's infix form yields a
    `relativedelta`, which is not JSON-serializable.
  * `COUNT(*) FILTER (WHERE p)` -> `COUNTIF(p)`.
  * `EXPLAIN` -> `warehouse.dry_run()`; BigQuery rejects EXPLAIN outright.
  * ASC ordering: DuckDB puts NULLs last in BOTH directions, BigQuery puts them
    FIRST on ASC. Every ASC `ORDER BY` here carries an explicit `NULLS LAST`.
    DESC already agrees in both engines — the existing `DESC NULLS LAST` clauses
    are correct as written and must not be "simplified".

Logical table names (`sales_daily`, `forecast_weekly`, ...) are still written as
bare identifiers. `BigQueryWarehouse.execute_sql` injects the backing CTEs
outermost, so nothing in this module needs injection awareness.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..bq import LOGICAL_TABLES
from ..column_roles import (
    ColumnNotFound,
    ResolvedColumn,
    resolve_column,
    table_exists,
)
from ..config import get_settings
from ..formatting import (
    make_error_response,
    make_kv_response,
    make_table_response,
)
from ..logging_setup import get_logger
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

log = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Column-resolution helpers (Patch #4)
# --------------------------------------------------------------------------------------
#
# Column resolution happens at *call time*, against the live BigQuery schema of
# each logical table's CTE body (a cached 0-byte dry run). See column_roles.

# The server no longer ingests anything, so "not loaded yet" is never actionable
# advice. Every logical table is a view over a BigQuery table that an
# independent pipeline fills.
_PIPELINE_NOTE = (
    "the upstream Kiteworks -> GCS -> BigQuery pipeline loads it daily around "
    "06:47 UTC"
)


def _backing_source(table: str) -> str | None:
    """Fully-qualified BigQuery table behind a logical name, if it is registered."""
    entry = LOGICAL_TABLES.get(table)
    return entry.primary_base_table if entry is not None else None


def _missing_table_error(
    *, table: str, fmt: str, hint: str | None = None
) -> ToolResponse:
    src = _backing_source(table)
    if src is not None:
        why = f"{table!r} is backed by `{src}`, but {_PIPELINE_NOTE} and has "
        why += "not landed data for this window"
    else:
        why = (
            f"{table!r} is not a registered logical table — known tables are "
            f"{sorted(LOGICAL_TABLES)}"
        )
    return make_error_response(
        code="DATA_UNAVAILABLE",
        message=why + (f". {hint}" if hint else ""),
        details={"dataset": table, "backing_table": src},
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


def _as_pydate(v: Any) -> date | None:
    """Normalize a driver date-ish value (date, datetime, ISO string) to `date`.

    BigQuery returns `datetime.date` for DATE columns, but a DATETIME leaking in
    from an un-ported `DATE +/- INTERVAL` would make `datetime > date + timedelta`
    raise TypeError inside a bare `except` and silently degrade a tool.
    """
    from datetime import datetime as _datetime

    if v is None:
        return None
    if isinstance(v, _datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _effective_date_range(
    warehouse: Warehouse, table: str, date_expr: str, where_sql: str = ""
) -> tuple[date | None, date | None]:
    """MIN/MAX of `date_expr` as DATEs under `where_sql`. Best-effort (Patch #12)."""
    try:
        _, dr = warehouse.execute_sql(
            f"SELECT MIN(SAFE_CAST({date_expr} AS DATE)), "
            f"MAX(SAFE_CAST({date_expr} AS DATE)) "
            f"FROM {quote_ident(table)} {where_sql}"
        )
        if dr:
            return _as_pydate(dr[0][0]), _as_pydate(dr[0][1])
    except Exception:
        pass
    return None, None


def _alternative_sales_source(
    warehouse: Warehouse, chosen: str
) -> dict[str, Any] | None:
    """Coverage of the sales table NOT chosen, so callers can see when the
    other grain reaches further back and opt in via `grain` (Patch #12)."""
    other = "sales_daily" if chosen == "sales_weekly" else "sales_weekly"
    if not table_exists(warehouse, other):
        return None
    alt_date = _try_resolve(warehouse, other, "date")
    if alt_date is None:
        return None
    mn, mx = _effective_date_range(warehouse, other, alt_date.select_as_date())
    return {
        "table": other,
        "min_date": str(mn) if mn else None,
        "max_date": str(mx) if mx else None,
        # The main response's effective range respects the call's filters;
        # this range deliberately does not (it answers "does the other grain
        # reach further back AT ALL").
        "scope": "entire table, unfiltered",
    }


# --------------------------------------------------------------------------------------
# Pre-flight gate for the two raw-SQL tools (replaces the DuckDB EXPLAIN step)
# --------------------------------------------------------------------------------------


def _human_bytes(n: int) -> str:
    """Scan sizes span KB to hundreds of GB; a fixed GB format renders a 1 MB
    threshold as the meaningless '0.00 GB'."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}"
    return f"{n} B"


def _dry_run_gate(
    warehouse: Warehouse, wrapped: str, *, fmt: str
) -> tuple[int | None, ToolResponse | None]:
    """Validate and price `wrapped` before executing it.

    BigQuery rejects `EXPLAIN` outright (`Statement not supported:
    ExplainStatement`), so the old planner gate could not simply be re-spelled.
    A dry run is strictly better than the EXPLAIN it replaces: it keeps the
    syntax/name validation AND returns `total_bytes_processed` at zero cost,
    which matters far more on per-byte billing than it did on a local file.
    Deleting the gate outright — the tempting move — would lose both halves.

    Returns `(estimated_bytes, None)` when the query may proceed, or
    `(estimated_bytes_or_None, error_response)` when it must not.
    """
    settings = get_settings()
    try:
        job = warehouse.dry_run(wrapped)
    except Exception as e:
        return None, make_error_response(
            code="SQL_PLAN_FAILED",
            message=f"dry run failed: {e}",
            details={"sql": wrapped[:2000]},
            fmt=fmt,
        )
    estimated = int(getattr(job, "total_bytes_processed", 0) or 0)
    if estimated > int(settings.bpd_bq_warn_bytes):
        log.warning(
            "expensive_query",
            estimated_bytes=estimated,
            warn_bytes=int(settings.bpd_bq_warn_bytes),
        )
    limit = int(settings.bpd_bq_max_bytes_billed)
    if estimated > limit:
        return estimated, make_error_response(
            code="QUERY_TOO_EXPENSIVE",
            message=(
                f"query would scan {_human_bytes(estimated)}, above the "
                f"{_human_bytes(limit)} limit (BPD_BQ_MAX_BYTES_BILLED). Narrow "
                "the date range or add a tcin/location filter."
            ),
            details={"estimated_bytes": estimated, "max_bytes_billed": limit},
            fmt=fmt,
        )
    return estimated, None


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

    # `wrap_with_limit` runs BEFORE execute_sql, and the logical-table CTEs are
    # injected INSIDE execute_sql, outermost — so they stay in scope for the
    # whole statement, including inside `_bpd_sub`. Do not inject here.
    wrapped = wrap_with_limit(cleaned, params.limit)

    estimated_bytes, gate_error = _dry_run_gate(
        read_only_warehouse, wrapped, fmt=params.response_format
    )
    if gate_error is not None:
        return gate_error

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
        extra={
            "row_count": len(dict_rows),
            "columns": cols,
            "limit": params.limit,
            # Surfaced so cost is visible in the transcript, not on a bill weeks
            # later.
            "estimated_bytes_scanned": estimated_bytes,
        },
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
        # row_count is the PRIMARY BASE TABLE's count, not a count through the
        # logical table's body (a real count would run the whole registry body
        # on every describe). For the reduced feeds those differ by a lot —
        # orders_daily reports ~147k base rows against ~7.7k logical rows — so
        # label the basis and print latest_state_note. Rendering the bare number
        # as "rows" states a 19x overstatement as fact in the first tool a user
        # runs. bq.describe()'s docstring requires a renderer to surface both.
        basis = body.get("row_count_basis")
        source = body.get("source")
        if basis == "base_table":
            suffix = f"~{body['row_count']:,} base rows"
            if source:
                suffix += f" in {source}"
        else:
            suffix = f"{body['row_count']:,} rows"
        parts.append(f"\n#### `{name}` ({suffix})")
        note = body.get("latest_state_note")
        if note:
            parts.append(f"> {note}")
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
        # WEEK(MONDAY) is deliberate bug-for-bug parity with DuckDB, NOT a claim
        # about Target's fiscal calendar. DuckDB's date_trunc('week', x) is
        # Monday-anchored; BigQuery's bare WEEK defaults to SUNDAY. Verified:
        # 2026-05-02 (Sat) -> 2026-04-27 under WEEK(MONDAY) and under DuckDB,
        # 2026-04-26 under bare WEEK.
        #
        # Target's real fiscal week is Sunday-start / Saturday-end (verified:
        # FISCAL_WEEK_BEGIN_D is Sunday in 100% of rows; every weekly sales /
        # inventory / GM date is Saturday in 100% of rows). So this buckets a
        # Saturday week-END back to the PRECEDING Monday. That oddity predates
        # the BigQuery swap and is preserved on purpose: a data-layer swap must
        # not move a single reported figure. FOLLOW-UP: decide WEEK(SUNDAY) on
        # its own merits, separately.
        #
        # Do NOT harmonize this with the +/- 6 day anchoring in
        # get_forecast_vs_actual — that is Target's fiscal week-END anchor and
        # is a different concept.
        #
        # Note the ARGUMENT ORDER FLIP. Getting it wrong is not a compile error:
        # DATE_TRUNC(x, WEEK) parses fine and silently shifts every bucket by a
        # day, moving Saturday-anchored rows into a different week.
        bucket = f"DATE_TRUNC({date_expr}, WEEK(MONDAY))"
    else:
        bucket = f"DATE_TRUNC({date_expr}, MONTH)"

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
            # NULLS LAST: DuckDB sorts NULLs last in both directions, BigQuery
            # puts them FIRST on ASC. The partial-bucket logic below no longer
            # depends on row position, but the rendered table should still read
            # the way it always has.
            "GROUP BY bucket ORDER BY bucket NULLS LAST"
        )
    else:
        sql = (
            f"SELECT {bucket} AS bucket, "
            f"SUM({quote_ident(units_col.name)}) AS total_units, "
            f"SUM({quote_ident(dollars_col.name)}) AS total_dollars "
            f"FROM {quote_ident(table)} {where_sql} "
            "GROUP BY bucket ORDER BY bucket NULLS LAST"
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

    # Patch #12 honesty: the data range actually covered, boundary-bucket
    # flags, and the other sales table's coverage — a May-partial month must
    # never read like a full month, and a no-arg call must say what period it
    # spans.
    eff_min, eff_max = _effective_date_range(warehouse, table, date_expr, where_sql)
    source_grain = "day" if table == "sales_daily" else "week"
    if dict_rows and params.grain in ("week", "month") and eff_min and eff_max:
        # Conservative ±6d tolerance when buckets are built from weekly rows:
        # a week-END anchor landing days into the bucket is still full coverage.
        tol = timedelta(days=6) if source_grain == "week" else timedelta(days=0)

        def _bounds(bucket: Any) -> tuple[date, date] | None:
            b = _as_pydate(bucket)
            if b is None:
                return None
            if params.grain == "week":
                return b, b + timedelta(days=6)
            nxt = (b.replace(day=28) + timedelta(days=4)).replace(day=1)
            return b, nxt - timedelta(days=1)

        # Anchor the boundary flags on the EARLIEST and LATEST dated buckets by
        # VALUE, not by row position. Under DuckDB a NULL-date bucket sorted
        # last, so scanning from either end happened to find a dated bucket;
        # under BigQuery an ASC sort puts NULLs first, and position-based
        # first/last would attach `partial_bucket=True` to the wrong end. Doing
        # it by value makes the result independent of the engine's null
        # ordering altogether. The NULL bucket's own flag stays None
        # (unknowable) rather than False.
        for r in dict_rows:
            r["partial_bucket"] = False if _bounds(r["bucket"]) else None
        dated = [r for r in dict_rows if _bounds(r["bucket"]) is not None]
        first = min(dated, key=lambda r: _bounds(r["bucket"])[0], default=None)
        last = max(dated, key=lambda r: _bounds(r["bucket"])[1], default=None)
        if first is not None:
            fb = _bounds(first["bucket"])
            if eff_min > fb[0] + tol:
                first["partial_bucket"] = True
        if last is not None:
            lb = _bounds(last["bucket"])
            if eff_max < lb[1] - tol:
                last["partial_bucket"] = True

    extra: dict[str, Any] = {
        "table": table,
        "source_grain": source_grain,
        "date_col": date_col.name,
        "date_col_type": date_col.sql_type,
        "units_col": units_col.name,
        "dollars_col": dollars_col.name if dollars_col else None,
        "requested_start": str(params.start_date) if params.start_date else None,
        "requested_end": str(params.end_date) if params.end_date else None,
        "effective_start": str(eff_min) if eff_min else None,
        "effective_end": str(eff_max) if eff_max else None,
        "alternative_source": _alternative_sales_source(warehouse, table),
        "sql": sql,
    }
    if params.grain == "month" and source_grain == "week":
        extra["week_straddle_note"] = (
            "month buckets are built from WEEKLY rows: a week straddling a "
            "month boundary is attributed wholly to the month of its "
            f"{date_col.name} anchor"
        )
    range_str = f", {eff_min}..{eff_max}" if eff_min and eff_max else ""
    return make_table_response(
        rows=dict_rows,
        columns=[*cols, "partial_bucket"] if dict_rows and "partial_bucket" in dict_rows[0] else cols,
        title=f"Sales summary ({params.grain}, table={table}{range_str})",
        extra=extra,
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
    # Patch #12: a no-arg call silently spanned all loaded history with no
    # indication of the period covered — echo the effective range.
    eff_min, eff_max = _effective_date_range(warehouse, table, date_expr, where_sql)
    range_str = f", {eff_min}..{eff_max}" if eff_min and eff_max else ""
    return make_table_response(
        rows=dict_rows,
        columns=cols,
        title=f"Top {params.top_n} SKUs by {params.by} (table={table}{range_str})",
        extra={
            "table": table,
            "metric_col": metric_col.name,
            "metric_role": metric_role,
            "requested_start": str(params.start_date) if params.start_date else None,
            "requested_end": str(params.end_date) if params.end_date else None,
            "effective_start": str(eff_min) if eff_min else None,
            "effective_end": str(eff_max) if eff_max else None,
            "alternative_source": _alternative_sales_source(warehouse, table),
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

    # Patch #12 staleness: 'latest known per pair' silently carries old rows
    # forward across feed gaps. Staleness is measured against the feed's
    # newest date WITHIN the as_of window (review fix: anchoring to the
    # whole-table max made any historical as_of return zero rows), and pairs
    # staler than max_staleness_days can be excluded.
    anchor_where = f"WHERE SAFE_CAST({date_expr} AS DATE) <= DATE '{as_of.isoformat()}'"
    stale_filter = ""
    if params.max_staleness_days is not None:
        # DATE_SUB, not `- INTERVAL n DAY`: the infix form returns DATETIME in
        # BigQuery, so `dt >= <DATETIME>` would compare a DATE against a
        # DATETIME and shift the boundary by the implicit midnight.
        stale_filter = (
            f"AND dt >= DATE_SUB((SELECT MAX(SAFE_CAST({date_expr} AS DATE)) "
            f"FROM {quote_ident(table)} {anchor_where}), "
            f"INTERVAL {int(params.max_staleness_days)} DAY)"
        )
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
        FROM ranked WHERE rn = 1 {stale_filter}
        ORDER BY tcin NULLS LAST, location_id NULLS LAST
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
    dict_rows = _rows_to_dicts(out_cols, rows)

    # Freshness anchor = the feed's newest date within the as_of window.
    _mn, window_max = _effective_date_range(warehouse, table, date_expr, anchor_where)
    staleness: dict[str, Any] = {
        "window_max_date": str(window_max) if window_max else None,
        "as_of": as_of.isoformat(),
        # Staleness is per-pair vs the feed's newest in-window day; feed lag
        # (as_of vs that day) is a separate number — surfaced so "0 days
        # stale" next to a newer as_of can't read as same-day data.
        "feed_lag_days_vs_as_of": (
            (as_of - window_max).days if window_max is not None else None
        ),
        "max_staleness_days_filter": params.max_staleness_days,
    }
    if window_max is not None and dict_rows:
        def _days_old(v: Any) -> int | None:
            d = _as_pydate(v)
            return (window_max - d).days if d is not None else None

        ages = [a for a in (_days_old(r["as_of_date"]) for r in dict_rows) if a is not None]
        stale = [a for a in ages if a > 7]
        staleness["returned_pairs"] = len(dict_rows)
        staleness["stale_pairs_over_7d"] = len(stale)
        staleness["max_staleness_days_returned"] = max(ages) if ages else None
        if stale:
            staleness["note"] = (
                "stale pairs are 'latest known' carried across feed gaps — "
                "their on_hand may be weeks old; filter with max_staleness_days "
                "or check inventory_weekly for gap windows"
            )
    return make_table_response(
        rows=dict_rows,
        columns=out_cols,
        title=f"Inventory snapshot as of {as_of.isoformat()} (table={table})",
        extra={
            "table": table,
            "date_col": date_col.name,
            "date_col_type": date_col.sql_type,
            "on_hand_col": on_hand_col.name,
            "staleness": staleness,
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

    # Patch #12: optionally drop inventory pairs whose latest snapshot is
    # staler than max_staleness_days vs the table's newest date — weeks-of-
    # supply from 10-week-old on-hand is misleading. When the filter is on,
    # the final join tightens to INNER so a filtered-out pair is EXCLUDED
    # (review fix: with the LEFT JOIN it resurfaced as on_hand=0 →
    # sell_through_rate=1.0, reading as fully sold through).
    inv_stale_filter = ""
    inv_join_kw = "LEFT JOIN"
    if params.max_staleness_days is not None:
        inv_stale_filter = (
            f"AND inv_dt >= DATE_SUB((SELECT MAX(SAFE_CAST({inv_date_expr} AS DATE)) "
            f"FROM {quote_ident(inv_table)}), "
            f"INTERVAL {int(params.max_staleness_days)} DAY)"
        )
        inv_join_kw = "JOIN"

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
                       {inv_date_expr} AS inv_dt,
                       ROW_NUMBER() OVER (
                           PARTITION BY {quote_ident(inv_tcin.name)}, {quote_ident(inv_loc.name)}
                           ORDER BY {inv_date_expr} DESC
                       ) AS rn
                FROM {quote_ident(inv_table)}
            ) AS ranked_inv WHERE rn = 1 {inv_stale_filter}
        )
        -- The NULLIF / CASE division guards below are NOT removable
        -- boilerplate. DuckDB evaluates 1/0 to inf; BigQuery raises
        -- "division by zero" and FAILS THE WHOLE QUERY. They are outage
        -- prevention now. Hold any new division to the same standard.
        --
        -- `* 1.0` promotes INT64 to FLOAT64. Every measure a role can currently
        -- reach is INT64 or FLOAT64, so that holds; if a role ever resolves to
        -- a NUMERIC column (e.g. dim_product.current_price) the product stays
        -- NUMERIC and division rounds to 9 decimals instead.
        --
        -- FULL/LEFT JOIN ... USING (k) is kept verbatim: BigQuery coalesces the
        -- USING keys exactly as DuckDB does, and qualified refs alongside USING
        -- are legal. Rewriting USING -> ON would drop the coalescing and NULL
        -- the spine on the unmatched side.
        SELECT s.tcin, s.location_id, s.units_sold, latest_inv.on_hand,
               CASE WHEN s.units_sold IS NULL OR s.units_sold = 0 THEN NULL
                    ELSE (latest_inv.on_hand * 1.0)
                         / NULLIF(s.units_sold / NULLIF(s.weeks_observed, 0), 0)
               END AS weeks_of_supply,
               CASE WHEN (s.units_sold + COALESCE(latest_inv.on_hand, 0)) = 0 THEN NULL
                    ELSE s.units_sold * 1.0
                         / (s.units_sold + COALESCE(latest_inv.on_hand, 0))
               END AS sell_through_rate
        FROM s {inv_join_kw} latest_inv USING (tcin, location_id)
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
            "inventory_max_date": str(
                _effective_date_range(warehouse, inv_table, inv_date_expr)[1] or ""
            ) or None,
            "max_staleness_days_filter": params.max_staleness_days,
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

    orders_daily is a delta feed that DuckDB materialized as LATEST STATE: each
    load replaced matching rows on the natural key (purchase_order_id, tcin,
    receiving_location_id), so the table held exactly one row — the last-known
    state — per PO line. BigQuery keeps every snapshot instead, so the
    latest-state reduction now lives in the logical table's own CTE body
    (a QUALIFY ROW_NUMBER() ... ORDER BY snapshot_d DESC = 1 in
    bq.LOGICAL_TABLES["orders_daily"]). This tool therefore still reads the
    whole table with no snapshot filter of its own, and must NOT add one —
    that would double-apply the reduction. Verified: the reduction is the
    difference between ~498k open units and ~14.2M.

    There is no physical "open units" column (and `purchase_order_active_f` is
    98.1% Target's literal `""` placeholder — filtering on it would drop nearly
    the entire order book), so open units are DERIVED per line as
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
    lines_cte = (
        f"WITH lines AS ("
        f"SELECT {quote_ident(tcin_col.name)} AS tcin, "
        f"{quote_ident(po_id.name)} AS po_id, "
        f"{open_expr} AS open_units "
        f"FROM {quote_ident(table)} {where_sql}"
        ") "
    )
    sql = (
        lines_cte
        + "SELECT tcin, COUNT(DISTINCT po_id) AS po_count, "
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

    # Patch #12: over-received lines (received + cancel > ordered) are
    # correctly excluded from open units, but must not be invisible — surface
    # a labeled count instead of a silent filter.
    over_received: dict[str, Any] | None = None
    try:
        _, ov = warehouse.execute_sql(
            lines_cte
            + "SELECT COUNT(*), COALESCE(SUM(-open_units), 0) "
            "FROM lines WHERE open_units < 0"
        )
        if ov:
            over_received = {"lines": ov[0][0], "units_over": ov[0][1]}
    except Exception:
        over_received = None

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
            "over_received": over_received,
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
        # (STRING ISO date, DATE, or TIMESTAMP). SAFE_CAST, not CAST: several
        # bpd_raw date columns ship as STRING holding Target's literal `""`
        # placeholder, and a plain CAST is a hard 400 `Invalid date`. Wrapped so
        # one table's bad date value degrades to skipped_tables instead of
        # aborting the tool (review fix).
        snap_day = f"SAFE_CAST({snap.select_as_date()} AS DATE)"
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
        # CURRENT_DATE() is already UTC in BigQuery, matching the
        # `SET TimeZone='UTC'` session pin the DuckDB warehouse used to set —
        # so no timezone argument, deliberately.
        #
        # `current_date + INTERVAL '3 weeks'` was DuckDB-only twice over: the
        # quoted plural interval literal does not parse in BigQuery, and the
        # infix form would return DATETIME even if it did.
        #
        # The snapshot filter below is the ONLY thing keeping these two
        # unpartitioned accumulating-snapshot tables affordable (po_plan_daily
        # 5.86M rows over 118 business_d values, po_plan_biweekly 9.39M over
        # 34). Never remove it.
        where = [
            f"{snap_day} = DATE '{latest_iso}'",
            f"{order_expr} >= CURRENT_DATE()",
            f"{order_expr} < DATE_ADD(CURRENT_DATE(), "
            f"INTERVAL {int(params.weeks_forward)} WEEK)",
        ]
        tcin_filter = _in_list_sql(tcin_col.name, params.tcin_filter)
        if tcin_filter:
            where.append(tcin_filter)
        projections.append(
            f"SELECT {quote_ident(tcin_col.name)} AS tcin, "
            # WEEK(MONDAY): argument order flips vs DuckDB's
            # date_trunc('week', x) AND the default anchor differs (bare WEEK
            # is Sunday-anchored). Bug-for-bug parity with DuckDB is
            # deliberate — see the long note in get_sales_summary.
            f"DATE_TRUNC({order_expr}, WEEK(MONDAY)) AS week, "
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
            # Patch #12: snapshot age makes plan-vs-plan divergence
            # diagnosable at a glance (the 07-29/07-31 launch-buy incident).
            "snapshot_age_days": (date.today() - date.fromisoformat(latest_iso)).days,
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
                f"po_plan table(s) {empty_tables} are registered but returned no "
                f"rows — {_PIPELINE_NOTE}, or the feed may have nothing planned "
                "in the requested window."
            ),
            details={"empty_tables": empty_tables},
            fmt=fmt,
        )

    union_sql = " UNION ALL ".join(f"({p})" for p in projections)
    sql = (
        f"WITH planned AS ({union_sql}) "
        "SELECT tcin, week, source, SUM(qty) AS planned_units, "
        "COUNT(*) AS line_count "
        "FROM planned GROUP BY tcin, week, source "
        "ORDER BY week NULLS LAST, tcin NULLS LAST, source NULLS LAST"
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
    ages = [v["snapshot_age_days"] for v in resolved_cols.values()]
    if len(ages) >= 2:
        divergence = max(ages) - min(ages)
        extra["snapshot_divergence_days"] = divergence
        if divergence > 1:
            extra["divergence_note"] = (
                "the two plans' snapshots differ by more than a day — POs cut "
                "in the gap appear as planned units in the older snapshot and "
                "as firm orders (bpd_get_open_orders) in the newer one, so the "
                "plans can legitimately disagree on launch buys. Prefer "
                "po_plan_daily for the near horizon; use po_plan_biweekly only "
                "beyond the daily plan's reach."
            )
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


def _classify_forecast_drops(
    warehouse: Warehouse, week_begin_day: str, snap_day: str
) -> list[dict[str, Any]]:
    """Per-snapshot drop summary for forecast_weekly (Patch #11).

    Target ships two structurally different drops into the same file pattern:
    weekly retrospectives (one week, published AFTER it) and forward horizons
    (many weeks, published before they start). Classified at query time from
    whatever rows survived ingest — nothing is persisted, because the per-key
    overwrite retention would stale any parse-time stamp. Consumers wanting
    "Target's current forward forecast" should read the most recent
    forward_horizon drop — NOT max(last_update_d), which is usually a tiny
    retrospective file (the §6 10x-understatement trap).
    """
    from datetime import timedelta as _td

    _, rows = warehouse.execute_sql(
        f"SELECT {snap_day} AS snap, "
        f"COUNT(DISTINCT {week_begin_day}) AS horizon_weeks, "
        f"MIN({week_begin_day}) AS min_week, "
        f"MAX({week_begin_day}) AS max_week, "
        "COUNT(*) AS n_rows "
        # NULLS LAST keeps a NULL-snapshot group at the END of the ascending
        # list, where DuckDB put it — the caller slices `[-40:]` for "the
        # newest 40 drops", so BigQuery's default NULLS-FIRST on ASC would
        # quietly change which rows survive that slice.
        "FROM forecast_weekly GROUP BY snap ORDER BY snap NULLS LAST"
    )
    out: list[dict[str, Any]] = []
    for raw_snap, horizon_weeks, raw_min_week, raw_max_week, n_rows in rows:
        # Normalize before any Python date arithmetic: a DATETIME leaking in
        # from a mis-ported expression would make `datetime > date + timedelta`
        # raise TypeError, which the caller's bare `except` would swallow into
        # a one-element error dict that still reads like a successful call.
        snap = _as_pydate(raw_snap)
        min_week = _as_pydate(raw_min_week)
        max_week = _as_pydate(raw_max_week)
        # Live-validated against Target's real publication timing (Patch #12):
        #   retro weeklies publish EXACTLY 7 days after week-begin (the Sunday
        #   after the week ends) — snap > max_week + 6 means every covered
        #   week had already ENDED at publication;
        #   forward drops publish the MONDAY after the Sunday week-begin
        #   (snap = min_week + 1), and per-key overwrites decay old forward
        #   drops to residues with snap < min_week — both are snap ≤ min+2.
        # The earlier +7d forward tolerance swallowed the retro pattern and
        # labeled every drop forward_horizon; retro must be tested FIRST on
        # the week-END side.
        if snap is None or min_week is None:
            kind = "anomalous"
        elif snap > max_week + _td(days=6):
            kind = "weekly_retrospective"
        elif snap <= min_week + _td(days=2):
            kind = "forward_horizon"
        else:
            kind = "anomalous"  # published mid-range of its covered weeks
        out.append(
            {
                "last_update_d": str(snap),
                "drop_kind": kind,
                "horizon_weeks": horizon_weeks,
                "min_week_begin": str(min_week),
                "max_week_begin": str(max_week),
                "rows": n_rows,
            }
        )
    return out


async def get_forecast_vs_actual(
    warehouse: Warehouse, params: ForecastVsActualInput
) -> ToolResponse:
    """Join Target's DFE weekly forecast with sales_weekly actuals (Patch #11).

    Design points (all from the verified defect-spec review):
      * Coverage-honest spine: both sides are aggregated to the finest common
        grain — (tcin, location, week) when both tables have a location column
        — and only MATCHED cells produce variance rows. Unmatched cells
        (forecast with no actuals, or vice versa) are never zero-filled into
        fake -100%/+inf variances; they are counted in `extra.coverage` and
        returned as rows only when `include_unmatched=true`.
      * snapshot_policy: 'latest_available' (default — ingest retains exactly
        one snapshot per key anyway, since last_update_d is not in the natural
        key) or 'pre_week' (only snapshots published before each week began —
        Target's true pre-week prediction; weeks whose forecast was published
        post-hoc become unmatched). An explicit as_of_date overrides both with
        a fixed cutoff.
      * weeks_back is clamped to actuals coverage and the effective range is
        echoed — a 12-week ask over 8 weeks of data reports itself instead of
        silently truncating (and both sides window on the same canonical
        Saturday week-end anchor).
      * variance_pct is a true percentage (x100, 0-100 scale like Target's
        _percentage columns).
      * extra.forecast_drops classifies each forecast snapshot as
        weekly_retrospective / forward_horizon / anomalous.
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
    fc_snap = _try_resolve(warehouse, "forecast_weekly", "snapshot_date")
    fc_loc = _try_resolve(warehouse, "forecast_weekly", "location")
    act_loc = _try_resolve(warehouse, "sales_weekly", "location")

    fc_date_expr = fc_date.select_as_date()
    act_date_expr = act_date.select_as_date()

    # Patch #5: forecast_weekly uses Sunday-anchored fiscal_week_begin_d while
    # sales_weekly uses Saturday-anchored sales_date. Canonicalize both sides
    # to the Saturday week-END; keep a week-BEGIN expr for the pre-week policy
    # and drop classification.
    #
    # The 6 and the direction are CORRECT and must not be touched. Verified:
    # FISCAL_WEEK_BEGIN_D is a Sunday in 100% of 4,734,013 forecast rows, and
    # every weekly sales / inventory / GM date is a Saturday in 100% of rows,
    # so begin + 6 = the Saturday week-END. This is Target's fiscal week-end
    # anchor and is a DIFFERENT CONCEPT from the WEEK(MONDAY) bucketing in
    # get_sales_summary / get_upcoming_pos; harmonizing them would shift every
    # forecast cell by a week.
    #
    # DATE_ADD/DATE_SUB, not `+/- INTERVAL 6 DAY`: the infix form returns
    # DATETIME in BigQuery. The outer CAST the DuckDB version needed is gone
    # because DATE_ADD of a DATE already returns a DATE.
    fc_name_lower = fc_date.name.lower()
    fc_is_week_begin = "begin" in fc_name_lower or "start" in fc_name_lower
    if fc_is_week_begin:
        fc_week_end_expr = f"DATE_ADD({fc_date_expr}, INTERVAL 6 DAY)"
        fc_week_begin_expr = fc_date_expr
    else:
        fc_week_end_expr = fc_date_expr
        fc_week_begin_expr = f"DATE_SUB({fc_date_expr}, INTERVAL 6 DAY)"

    # ---- effective window: requested weeks_back clamped to actuals coverage.
    weeks_back = int(params.weeks_back)
    try:
        _, cov = warehouse.execute_sql(
            f"SELECT MIN(SAFE_CAST({act_date_expr} AS DATE)), "
            f"MAX(SAFE_CAST({act_date_expr} AS DATE)) FROM sales_weekly"
        )
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=f"actuals coverage probe failed: {e}",
            fmt=fmt,
        )
    act_min, act_max = (cov[0] if cov else (None, None))
    if act_min is None:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message=(
                "sales_weekly returned no rows — it is backed by "
                f"`{_backing_source('sales_weekly')}` and {_PIPELINE_NOTE}."
            ),
            fmt=fmt,
        )
    from datetime import timedelta as _td

    def _as_date(v: Any) -> date:
        return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])

    act_min, act_max = _as_date(act_min), _as_date(act_max)
    requested_start = date.today() - _td(weeks=weeks_back)
    requested_end = date.today()
    effective_start = max(requested_start, act_min)
    effective_end = min(requested_end, act_max)
    if effective_start > effective_end:
        return make_error_response(
            code="DATA_UNAVAILABLE",
            message=(
                f"requested window {requested_start} → {requested_end} does not "
                f"overlap actuals coverage {act_min} → {act_max}."
            ),
            details={
                "requested_start": str(requested_start),
                "requested_end": str(requested_end),
                "actuals_min": str(act_min),
                "actuals_max": str(act_max),
            },
            fmt=fmt,
        )
    truncated = effective_start > requested_start or effective_end < requested_end

    # ---- spine: (tcin, location, week) when both sides have a location.
    spine_has_location = fc_loc is not None and act_loc is not None
    if params.aggregate == "by_sku_location_week" and not spine_has_location:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message="by_sku_location_week requires a location column on both tables.",
            fmt=fmt,
        )
    if params.location_filter and not spine_has_location:
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "location_filter supplied but one of the tables lacks a "
                f"location column (forecast loc={fc_loc and fc_loc.name}, "
                f"sales loc={act_loc and act_loc.name})"
            ),
            fmt=fmt,
        )

    # ---- per-side filters (tcin/location/effective window).
    # These date predicates are load-bearing for COST as well as correctness:
    # BigQuery pushes them down into the injected CTEs and partition pruning
    # survives (sales_daily 13.3 MB -> 3.5 MB, inventory_daily 38.9 MB ->
    # 3.2 MB). Dropping one as a "simplification" is a ~10x cost regression.
    fc_where: list[str] = [
        f"SAFE_CAST({fc_week_end_expr} AS DATE) >= DATE '{effective_start.isoformat()}'",
        f"SAFE_CAST({fc_week_end_expr} AS DATE) <= DATE '{effective_end.isoformat()}'",
    ]
    act_where: list[str] = [
        f"SAFE_CAST({act_date_expr} AS DATE) >= DATE '{effective_start.isoformat()}'",
        f"SAFE_CAST({act_date_expr} AS DATE) <= DATE '{effective_end.isoformat()}'",
    ]
    if params.tcin_filter:
        tcin_in = ",".join(str(int(v)) for v in params.tcin_filter)
        fc_where.append(f"{quote_ident(fc_tcin.name)} IN ({tcin_in})")
        act_where.append(f"{quote_ident(act_tcin.name)} IN ({tcin_in})")
    if params.location_filter:
        loc_in = ",".join(str(int(v)) for v in params.location_filter)
        fc_where.append(f"{quote_ident(fc_loc.name)} IN ({loc_in})")
        act_where.append(f"{quote_ident(act_loc.name)} IN ({loc_in})")

    # ---- snapshot policy → optional cutoff inside the ranked CTE.
    # A cutoff is only enforceable when a snapshot column exists; requesting
    # one against a table that lacks it is a hard error, never a silent no-op
    # with metadata claiming otherwise (adversarial-review fix).
    policy = params.snapshot_policy
    # A non-default lead only means something under pre_week (and as_of_date
    # overrides the policy entirely) — silently dropping it would let a
    # forgotten snapshot_policy='pre_week' read post-hoc revisions as a
    # week-out forecast (review fix: hard error, never a silent no-op).
    if params.pre_week_min_lead_days != 1 and (
        policy != "pre_week" or params.as_of_date is not None
    ):
        return make_error_response(
            code="INVALID_ARGUMENT",
            message=(
                "pre_week_min_lead_days only applies with "
                "snapshot_policy='pre_week' and without as_of_date (which "
                "overrides the policy). Set snapshot_policy='pre_week' or "
                "drop the lead parameter."
            ),
            fmt=fmt,
        )
    if fc_snap is None and (params.as_of_date is not None or policy == "pre_week"):
        return make_error_response(
            code="SCHEMA_INCOMPATIBLE",
            message=(
                "snapshot_policy='pre_week' / as_of_date need a forecast "
                "snapshot column (role 'snapshot_date', e.g. last_update_d), "
                "which forecast_weekly does not have — the cutoff cannot be "
                "enforced. Use the default snapshot_policy='latest_available'."
            ),
            fmt=fmt,
        )
    if params.as_of_date is not None:
        cutoff_sql: str | None = f"DATE '{params.as_of_date.isoformat()}'"
        cutoff_desc = f"fixed as_of_date {params.as_of_date.isoformat()}"
    elif policy == "pre_week":
        lead = int(params.pre_week_min_lead_days)
        # DATE_SUB/DATE_ADD: this pair had NO outer CAST in the DuckDB version,
        # so under BigQuery the infix form would yield a DATETIME and compare
        # `snapshot_date <= DATETIME`, silently shifting the cutoff.
        if lead >= 0:
            cutoff_sql = f"DATE_SUB({fc_week_begin_expr}, INTERVAL {lead} DAY)"
        else:
            cutoff_sql = f"DATE_ADD({fc_week_begin_expr}, INTERVAL {-lead} DAY)"
        cutoff_desc = (
            f"pre_week (snapshot at least {lead} day(s) before each week began)"
        )
    elif fc_snap is None:
        cutoff_sql = None
        cutoff_desc = "latest_available (table has no snapshot column)"
    else:
        cutoff_sql = None
        cutoff_desc = "latest_available (no snapshot cutoff)"

    spine_cols = ["tcin"] + (["location_id"] if spine_has_location else []) + ["week_end_date"]
    spine_key = ", ".join(spine_cols)

    if fc_snap is not None:
        snap_date_expr = fc_snap.select_as_date()
        # Snapshot dedup MUST run at the forecast table's OWN grain — include
        # the forecast location column whenever it exists, independent of the
        # spine. Partitioning only by the (coarser) spine collapsed every
        # location's forecast to one arbitrary row when sales_weekly was
        # chain-level (adversarial-review fix: critical). The units tiebreak
        # keeps equal-snapshot duplicates deterministic across queries.
        partition_cols = [quote_ident(fc_tcin.name), fc_week_end_expr]
        if fc_loc is not None:
            partition_cols.insert(1, quote_ident(fc_loc.name))
        snap_where = f"WHERE {snap_date_expr} <= {cutoff_sql} AND " if cutoff_sql else "WHERE "
        fc_source_cte = f"""
            ranked_fc AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY {", ".join(partition_cols)}
                           ORDER BY {snap_date_expr} DESC,
                                    {quote_ident(fc_units.name)} DESC
                       ) AS _snap_rn
                FROM forecast_weekly
                {snap_where}{" AND ".join(fc_where)}
            ),
            fc_src AS (SELECT * FROM ranked_fc WHERE _snap_rn = 1),"""
        fc_cells_from = "fc_src"
        fc_cells_where = ""
    else:
        fc_source_cte = ""
        fc_cells_from = "forecast_weekly"
        fc_cells_where = f"WHERE {' AND '.join(fc_where)}"

    fc_loc_proj = (
        f"{quote_ident(fc_loc.name)} AS location_id, " if spine_has_location else ""
    )
    act_loc_proj = (
        f"{quote_ident(act_loc.name)} AS location_id, " if spine_has_location else ""
    )

    cte_prefix = f"""
        WITH {fc_source_cte}
        fc_cells AS (
            SELECT {quote_ident(fc_tcin.name)} AS tcin, {fc_loc_proj}
                   SAFE_CAST({fc_week_end_expr} AS DATE) AS week_end_date,
                   COALESCE(SUM({quote_ident(fc_units.name)}), 0) AS forecast_units
            FROM {fc_cells_from}
            {fc_cells_where}
            GROUP BY {spine_key}
        ),
        act_cells AS (
            SELECT {quote_ident(act_tcin.name)} AS tcin, {act_loc_proj}
                   SAFE_CAST({act_date_expr} AS DATE) AS week_end_date,
                   COALESCE(SUM({quote_ident(act_units.name)}), 0) AS actual_units
            FROM sales_weekly
            WHERE {" AND ".join(act_where)}
            GROUP BY {spine_key}
        ),
        all_cells AS (
            SELECT {spine_key}, forecast_units, actual_units,
                   CASE WHEN forecast_units IS NOT NULL AND actual_units IS NOT NULL
                        THEN 'matched'
                        WHEN forecast_units IS NOT NULL THEN 'forecast_only'
                        ELSE 'actual_only' END AS coverage
            FROM fc_cells FULL OUTER JOIN act_cells USING ({spine_key})
        )
    """

    # Output grouping per the requested aggregate.
    if params.aggregate == "by_sku":
        group_cols = ["tcin"]
    elif params.aggregate == "by_sku_location_week":
        group_cols = ["tcin", "location_id", "week_end_date"]
    else:  # by_sku_week
        group_cols = ["tcin", "week_end_date"]
    group_key = ", ".join(group_cols)

    coverage_filter = "" if params.include_unmatched else "WHERE coverage = 'matched'"

    # ------------------------------------------------------------------
    # ONE JOB, NOT TWO. The result rollup and the coverage rollup both used to
    # be executed with their own copy of `cte_prefix`. Under BigQuery that is
    # two full scans: the forecast_weekly CTE alone reads 151.5 MB and its
    # QUALIFY blocks predicate pushdown, so pruning does not help — ~350 MB per
    # tool call. They are UNION ALL'd onto one statement with a `_kind`
    # discriminator and split back apart in Python below, which halves it.
    #
    # The two branches must be union-compatible, so the coverage branch pads
    # the grouping columns and the variance columns with typed NULLs. The
    # response's row/column shape is unchanged: `_kind` and the coverage rows
    # are stripped before anything is returned.
    # ------------------------------------------------------------------
    _NULL_FOR = {
        "tcin": "CAST(NULL AS INT64)",
        "location_id": "CAST(NULL AS INT64)",
        "week_end_date": "CAST(NULL AS DATE)",
    }
    result_cols = [
        *group_cols,
        "coverage",
        "forecast_units",
        "actual_units",
        "cell_count",
        "variance_units",
        "variance_pct",
    ]
    cov_pad = ", ".join(f"{_NULL_FOR[c]} AS {c}" for c in group_cols)
    # Ordering: result rows first (so the caller's slice of `rows` keeps its
    # historical order), then coverage rows. Within the result rows, the
    # original `ORDER BY {group_key}, coverage` — with NULLS LAST, because
    # BigQuery puts NULLs first on ASC where DuckDB put them last.
    order_asc = ", ".join(f"{c} NULLS LAST" for c in group_cols)
    sql = f"""{cte_prefix},
        res AS (
            SELECT {group_key}, coverage,
                   SUM(forecast_units) AS forecast_units,
                   SUM(actual_units) AS actual_units,
                   COUNT(*) AS cell_count,
                   SUM(actual_units) - SUM(forecast_units) AS variance_units,
                   -- The COALESCE(...)=0 guard is not boilerplate: DuckDB
                   -- evaluates 1/0 to inf, BigQuery raises "division by zero"
                   -- and fails the whole query.
                   CASE WHEN COALESCE(SUM(forecast_units), 0) = 0 THEN NULL
                        ELSE (SUM(actual_units) - SUM(forecast_units)) * 100.0
                             / SUM(forecast_units)
                   END AS variance_pct
            FROM all_cells
            {coverage_filter}
            GROUP BY {group_key}, coverage
            ORDER BY {order_asc}, coverage NULLS LAST
            LIMIT 2000
        ),
        cov AS (
            SELECT coverage, COUNT(*) AS cells,
                   SUM(forecast_units) AS forecast_units,
                   SUM(actual_units) AS actual_units
            FROM all_cells GROUP BY coverage
        )
        SELECT 'result' AS _kind, {group_key}, coverage,
               forecast_units, actual_units, cell_count,
               variance_units, variance_pct
        FROM res
        UNION ALL
        SELECT 'coverage' AS _kind, {cov_pad}, coverage,
               forecast_units, actual_units, cells AS cell_count,
               CAST(NULL AS FLOAT64) AS variance_units,
               CAST(NULL AS FLOAT64) AS variance_pct
        FROM cov
        ORDER BY _kind DESC, {order_asc}, coverage NULLS LAST
    """
    try:
        all_cols, all_rows = warehouse.execute_sql(sql)
    except Exception as e:
        return make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=fmt,
        )

    kind_ix = all_cols.index("_kind")
    out_cols = [c for c in all_cols if c != "_kind"]
    assert out_cols == result_cols, (out_cols, result_cols)
    cov_ix = {c: all_cols.index(c) for c in ("coverage", "forecast_units",
                                             "actual_units", "cell_count")}
    rows = [
        tuple(v for i, v in enumerate(r) if i != kind_ix)
        for r in all_rows
        if r[kind_ix] == "result"
    ]
    coverage_summary = {
        r[cov_ix["coverage"]]: {
            "cells": r[cov_ix["cell_count"]],
            "forecast_units": r[cov_ix["forecast_units"]],
            "actual_units": r[cov_ix["actual_units"]],
        }
        for r in all_rows
        if r[kind_ix] == "coverage"
    }

    # Best-effort honesty metadata: drop classification + snapshot lag.
    # Separate try/excepts (review fix): a lag-query failure must not discard
    # a successful classification or misattribute the error.
    forecast_drops: list[dict[str, Any]] | None = None
    snapshot_lag: dict[str, Any] | None = None
    snapshot_lag_error: str | None = None
    if fc_snap is not None:
        snap_day = f"SAFE_CAST({fc_snap.select_as_date()} AS DATE)"
        week_begin_day = f"SAFE_CAST({fc_week_begin_expr} AS DATE)"
        try:
            forecast_drops = _classify_forecast_drops(warehouse, week_begin_day, snap_day)
        except Exception as e:  # metadata must never break the tool
            forecast_drops = [{"error": f"drop classification failed: {e}"}]
        try:
            # DATE_DIFF, not `DATE - DATE`: BigQuery's infix subtraction
            # returns an INTERVAL, which arrives in Python as a
            # `relativedelta` and is not JSON-serializable — it would blow up
            # in the response encoder, not here.
            #
            # COUNTIF, not `COUNT(*) FILTER (WHERE ...)`: the FILTER clause is
            # a hard 400 syntax error in BigQuery. It sits inside the
            # try/except below, which degrades to `extra.snapshot_lag_error`
            # with ok=True — so a smoke test would never have caught it.
            _, lag = warehouse.execute_sql(
                f"SELECT MIN(DATE_DIFF({week_begin_day}, {snap_day}, DAY)), "
                f"MAX(DATE_DIFF({week_begin_day}, {snap_day}, DAY)), "
                f"COUNTIF({snap_day} > {week_begin_day}) "
                "FROM forecast_weekly"
            )
            if lag and lag[0][0] is not None:
                snapshot_lag = {
                    "min_lead_days": lag[0][0],
                    "max_lead_days": lag[0][1],
                    "post_hoc_rows": lag[0][2],
                }
        except Exception as e:
            snapshot_lag_error = f"snapshot lag probe failed: {e}"

    effective_weeks = ((effective_end - effective_start).days // 7) + 1
    title = (
        f"Forecast vs actual ({effective_start} → {effective_end}, "
        f"aggregate={params.aggregate}, matched"
        f"{'+unmatched' if params.include_unmatched else ' cells only'})"
    )
    extra: dict[str, Any] = {
        "snapshot_policy": cutoff_desc,
        "spine": "tcin, location, week" if spine_has_location else "tcin, week",
        "coverage": coverage_summary,
        "include_unmatched": params.include_unmatched,
        "requested_weeks_back": weeks_back,
        "requested_start": str(requested_start),
        "requested_end": str(requested_end),
        "effective_start": str(effective_start),
        "effective_end": str(effective_end),
        "effective_weeks_covered": effective_weeks,
        "window_truncated_to_actuals_coverage": truncated,
        "forecast_date_col": fc_date.name,
        "forecast_week_anchor": "begin" if fc_is_week_begin else "end",
        "forecast_week_shift_days": 6 if fc_is_week_begin else 0,
        "forecast_units_col": fc_units.name,
        "forecast_snapshot_col": fc_snap.name if fc_snap else None,
        "actual_date_col": act_date.name,
        "actual_units_col": act_units.name,
        "variance_pct_scale": "percent (0-100)",
        "sql": sql,
    }
    if cutoff_sql is not None:
        # Applies to ANY historical cutoff (pre_week or as_of_date) — the
        # thinness comes from retention, not from the policy chosen.
        #
        # FOLLOW-UP (out of scope for the data-layer swap, unverified): this
        # text describes DuckDB's per-key ingest retention, which no longer
        # exists. The BigQuery source is SCD2 with valid_from/valid_to, so
        # historical snapshots may in fact be recoverable and `pre_week`
        # backtesting may be genuinely possible. The caveat is therefore now
        # wrong IN THE USER'S FAVOUR — conservative, not misleading — so it is
        # left exactly as it was rather than being rewritten on a guess.
        extra["snapshot_retention_caveat"] = (
            "per-key ingest retention keeps only the NEWEST drop's row for "
            "each (tcin, location, week) — once a later forward or "
            "retrospective drop re-covers a week, its earlier snapshot is no "
            "longer in the warehouse. Historical-cutoff coverage is therefore "
            "thin for closed weeks and genuine only for weeks not yet "
            "re-covered; a durable backtest needs snapshot archiving (a "
            "deliberate retention change, not a query option)."
        )
    if forecast_drops is not None:
        # The list is snapshot-ascending; keep the NEWEST 40 — consumers are
        # directed to the most recent forward_horizon drop, which a head slice
        # would eventually truncate away (review fix).
        extra["forecast_drops"] = forecast_drops[-40:]
        if len(forecast_drops) > 40:
            extra["forecast_drops_total"] = len(forecast_drops)
    if snapshot_lag is not None:
        extra["snapshot_lag"] = snapshot_lag
    if snapshot_lag_error is not None:
        extra["snapshot_lag_error"] = snapshot_lag_error
    return make_table_response(
        rows=_rows_to_dicts(out_cols, rows),
        columns=out_cols,
        title=title,
        extra=extra,
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
    estimated_bytes, gate_error = _dry_run_gate(read_only_warehouse, wrapped, fmt=fmt)
    if gate_error is not None:
        return gate_error
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
        "estimated_bytes_scanned": estimated_bytes,
    }
    return make_kv_response(
        data=payload,
        title=f"Exported {len(rows):,} row(s) to {target.name}",
        fmt=fmt,
    )
