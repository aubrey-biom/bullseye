"""DTC (Shopify) and paid-media analytics — Phase 2 of the DTC performance /
pacing enhancement.

Three tools compose the eleven `dtc_*` / `ads_*` logical tables Phase 1 added
to the registry (the "DTC + ads" block of bq.py):

  * `get_dtc_sales_summary`   — orders, units, gross, admin net revenue and new
                                customers by period x channel_bucket.
  * `get_ads_performance`     — spend, impressions, clicks, conversions and the
                                ratios derived from them by period x channel
                                (or x campaign), with each period's delivery
                                integrity from media_delivery_status.
  * `get_marketing_efficiency` — the blended view: all-channel spend against
                                core-D2C sales and new customers per period
                                (MER, blended CAC, cost per order) beside the
                                platforms' own attributed ROAS.
  * `get_dtc_pacing`          — month-to-date pacing of demand, spend and new
                                customers against the ecomm team's daily
                                forecast (config/dtc_pacing_targets.json), the
                                prior period, last month and last year.

Definitions are the registry's, never re-derived here:

  * Channel scope is `channel_bucket` (bq.DTC_SOURCE_BUCKETS). Buckets the
    caller did not ask for are never dropped silently — their window totals
    come back in `extra.other_buckets`. That is how the ~$213K/90d of $0
    gifting orders valued at list price stays visible instead of quietly
    inflating a gross figure or quietly vanishing from it.
  * "Sales" means PAID orders (`is_paid_order`); units count product lines
    only (`is_product_line`). Unpaid orders are reported beside the paid ones.
  * Net revenue is `admin_net_revenue` from dtc_revenue_lines (the certified
    view), order-date basis; `refunds_allocated` is that view's allocation.
  * A new customer is a row of dtc_customer_first_order — the first PAID
    core-D2C order. It is core-D2C by construction, so it is attached to the
    core_d2c row only.
  * Every ratio (AOV, CTR, CPC, CPM, CPA, ROAS, MER, CAC) is computed from
    SUMS with SAFE_DIVIDE: never averaged from per-row ratios, and never a
    bare `/` (BigQuery raises on division by zero and fails the whole query).
  * Spend is the cross-channel spine `ads_spend_daily` — Google from the
    campaign fact only, so no sub-grain double count.
  * A zero-spend day is only a real zero when media_delivery_status says so.
    ABSENT_UNDIAGNOSED days are flagged, never zero-filled, and days the view
    has not classified at all are counted as `days_unclassified`.

Periods are Monday-anchored weeks (`DATE_TRUNC(x, WEEK(MONDAY))`) or calendar
months. DTC and ads have no Target fiscal calendar to honour, so this is the
ISO-style week and NOT the Sunday-Saturday Target week: do not join these rows
to `sales_weekly` on `period`.

Time: Shopify dates are Central (`order_date_ct`); ad dates are the platforms'
account-local day. "Today" for defaults is Central, and any period that
includes today, or that the requested window clips, is flagged
`partial_period` — ads land yesterday's data before 13:00 UTC, so today's
column is always short.

Every tool resolves its columns through `column_roles.resolve_column` at call
time (cached dry-run schema, 0 bytes), so an upstream rename surfaces as
SCHEMA_INCOMPATIBLE with the candidates tried, exactly like the Target tools.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..bq import DTC_SOURCE_BUCKETS
from ..column_roles import ColumnNotFound, ResolvedColumn, resolve_column, table_exists
from ..formatting import make_error_response, make_table_response, render_markdown_table
from ..logging_setup import get_logger
from ..pacing_targets import REFRESH_COMMAND, PacingTargets, TargetsUnavailable, load_targets
from ..schemas import (
    AdsPerformanceInput,
    DtcPacingInput,
    DtcSalesSummaryInput,
    MarketingEfficiencyInput,
    ToolResponse,
)
from ..warehouse import Warehouse, quote_ident
from .query import _as_pydate, _column_not_found_error, _missing_table_error, _rows_to_dicts

log = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

REPORTING_TZ = "America/Chicago"
CORE_BUCKET = "core_d2c"
UNKNOWN_BUCKET = "unknown"
ALL_BUCKETS: tuple[str, ...] = (*DTC_SOURCE_BUCKETS, UNKNOWN_BUCKET)

DEFAULT_LOOKBACK_DAYS = 90
ROW_CAP = 2000

#: fct_meta_performance's first day. Blended spend before this is Google-only.
META_HISTORY_START = date(2025, 7, 2)

DELIVERY_DELIVERED = "DELIVERED"
DELIVERY_ZERO: tuple[str, ...] = ("OBSERVED_ZERO", "CONFIRMED_NO_DELIVERY")
DELIVERY_UNDIAGNOSED = "ABSENT_UNDIAGNOSED"

# purchase_type placeholders. A join key must never be NULL (NULL never equals
# NULL, so a FULL JOIN USING it would pair nothing), hence constants.
_PT_ALL = "(all)"
_PT_NONE = "(none)"

WEEK_NOTE = (
    "period is a Monday-anchored week (DATE_TRUNC(x, WEEK(MONDAY))) — NOT Target's "
    "Sunday-Saturday fiscal week; do not join to sales_weekly on period"
)


# --------------------------------------------------------------------------------------
# Window and period helpers (pure python; unit-tested in the hermetic tier)
# --------------------------------------------------------------------------------------


def today_reporting() -> date:
    """Today in the reporting time zone (Central), not the container's UTC."""
    return datetime.now(ZoneInfo(REPORTING_TZ)).date()


def resolve_window(start: date | None, end: date | None) -> tuple[date, date]:
    """Apply the defaults: end = today (Central), start = 90 days ending at end (inclusive)."""
    end_d = end or today_reporting()
    start_d = start or end_d - timedelta(days=DEFAULT_LOOKBACK_DAYS - 1)
    return start_d, end_d


def period_expr(grain: str, date_expr: str) -> str:
    """The GROUP BY bucket for a DATE expression."""
    if grain == "day":
        return date_expr
    if grain == "week":
        # Argument order matters and getting it wrong is not a compile error:
        # DATE_TRUNC(x, WEEK) parses fine and anchors to SUNDAY instead.
        return f"DATE_TRUNC({date_expr}, WEEK(MONDAY))"
    return f"DATE_TRUNC({date_expr}, MONTH)"


def period_bounds(grain: str, period: date) -> tuple[date, date]:
    """First and last calendar day of the period `period` labels."""
    if grain == "day":
        return period, period
    if grain == "week":
        return period, period + timedelta(days=6)
    nxt = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    return period, nxt - timedelta(days=1)


def days_in_window(grain: str, period: date, start: date, end: date) -> int:
    """Calendar days of the period that fall inside [start, end]."""
    lo, hi = period_bounds(grain, period)
    lo, hi = max(lo, start), min(hi, end)
    return max((hi - lo).days + 1, 0)


def is_partial(grain: str, period: date, start: date, end: date, today: date) -> bool:
    """Is the period clipped by the window, or does it reach today (data still landing)?"""
    lo, hi = period_bounds(grain, period)
    return lo < start or hi > end or hi >= today


def _date_pred(date_expr: str, start: date, end: date) -> str:
    return f"{date_expr} BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'"


# --------------------------------------------------------------------------------------
# Warehouse helpers
# --------------------------------------------------------------------------------------


def _window_error(start: date, end: date, fmt: str) -> ToolResponse | None:
    if start > end:
        return make_error_response(
            code="INVALID_DATE_RANGE",
            message=f"start_date {start} is after end_date {end}",
            details={"start_date": str(start), "end_date": str(end)},
            fmt=fmt,
        )
    return None


def _require_tables(warehouse: Warehouse, tables: Iterable[str], fmt: str) -> ToolResponse | None:
    for t in tables:
        if not table_exists(warehouse, t):
            return _missing_table_error(table=t, fmt=fmt)
    return None


def _cols(warehouse: Warehouse, table: str, roles: Iterable[str]) -> dict[str, ResolvedColumn]:
    """role -> resolved column for one logical table. Raises ColumnNotFound."""
    return {r: resolve_column(warehouse, table, r) for r in roles}


def _q(c: ResolvedColumn) -> str:
    return quote_ident(c.name)


def _names(resolved: Mapping[str, Mapping[str, ResolvedColumn]]) -> dict[str, dict[str, str]]:
    return {t: {r: c.name for r, c in cols.items()} for t, cols in resolved.items()}


def _execute(
    warehouse: Warehouse, sql: str, fmt: str
) -> tuple[list[dict[str, Any]] | None, ToolResponse | None]:
    try:
        cols, rows = warehouse.execute_sql(sql)
    except Exception as e:
        return None, make_error_response(
            code="SQL_EXECUTION_FAILED",
            message=str(e),
            details={"sql": sql},
            fmt=fmt,
        )
    return _rows_to_dicts(cols, rows), None


def _stamp_periods(
    rows: list[dict[str, Any]], grain: str, start: date, end: date, today: date
) -> None:
    """Normalise `period` to a date and add `partial_period`, in place."""
    for r in rows:
        p = _as_pydate(r.get("period"))
        r["period"] = p
        r["partial_period"] = is_partial(grain, p, start, end, today) if p else None


def _window_extra(
    params: DtcSalesSummaryInput | AdsPerformanceInput | MarketingEfficiencyInput,
    start: date,
    end: date,
) -> dict[str, Any]:
    return {
        "grain": params.grain,
        "window": {
            "start": str(start),
            "end": str(end),
            "start_defaulted": params.start_date is None,
            "end_defaulted": params.end_date is None,
            "time_zone": REPORTING_TZ,
        },
        "week_note": WEEK_NOTE if params.grain == "week" else None,
    }


def _f(v: Any) -> float:
    return float(v or 0)


# --------------------------------------------------------------------------------------
# bpd_get_dtc_sales_summary
# --------------------------------------------------------------------------------------

_DTC_SALES_DEFINITIONS = {
    "orders": "COUNT(DISTINCT order_id) over lines where is_paid_order",
    "customers": "COUNT(DISTINCT customer_id) over paid lines (NULL ids not counted)",
    "new_customers": (
        "rows of dtc_customer_first_order in the period: a customer's first PAID "
        "core-D2C order. core_d2c row only; NULL when by_purchase_type"
    ),
    "units": "SUM(quantity) over paid lines where is_product_line",
    "gross_sales": "SUM(gross_line) over paid lines (gross_using_line_price)",
    "net_line_sales": "SUM(net_line) over paid lines (net_line_sales; refunds NOT subtracted)",
    "aov": "SAFE_DIVIDE(gross_sales, orders)",
    "gross_revenue": "SUM(revenue_amount) from dtc_revenue_lines (certified view), order-date basis",
    "refunds_allocated": "SUM(allocated_refund) from dtc_revenue_lines",
    "admin_net_revenue": (
        "SUM(admin_net_revenue) from dtc_revenue_lines — gross minus allocated discount "
        "minus allocated refund; the number leadership reconciles to"
    ),
    "unpaid_orders": "COUNT(DISTINCT order_id) over lines where NOT is_paid_order (order total <= $1)",
    "unpaid_list_value": (
        "SUM(gross_line) over unpaid lines — list value of $0 orders (gifting, comps). Not sales."
    ),
}

_DTC_SALES_MARKDOWN_COLUMNS = [
    "period",
    "bucket",
    "orders",
    "new_customers",
    "units",
    "gross_sales",
    "admin_net_revenue",
    "aov",
    "partial_period",
]

_OTHER_BUCKET_MEASURES = (
    "orders",
    "gross_sales",
    "admin_net_revenue",
    "unpaid_orders",
    "unpaid_list_value",
)


async def get_dtc_sales_summary(warehouse: Warehouse, params: DtcSalesSummaryInput) -> ToolResponse:
    fmt = params.response_format
    start, end = resolve_window(params.start_date, params.end_date)
    if err := _window_error(start, end, fmt):
        return err
    if err := _require_tables(
        warehouse, ("dtc_order_lines", "dtc_revenue_lines", "dtc_customer_first_order"), fmt
    ):
        return err
    try:
        o = _cols(
            warehouse,
            "dtc_order_lines",
            (
                "date",
                "order_id",
                "customer_id",
                "bucket",
                "paid",
                "product_line",
                "purchase_type",
                "units",
                "gross",
                "net",
            ),
        )
        r = _cols(
            warehouse,
            "dtc_revenue_lines",
            ("date", "bucket", "purchase_type", "gross", "net", "refund"),
        )
        n = _cols(warehouse, "dtc_customer_first_order", ("date", "customer_id"))
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    o_date = o["date"].select_as_date()
    r_date = r["date"].select_as_date()
    n_date = n["date"].select_as_date()
    paid, prod = _q(o["paid"]), _q(o["product_line"])
    if params.by_purchase_type:
        pt_o = f"COALESCE({_q(o['purchase_type'])}, '{_PT_NONE}')"
        pt_r = f"COALESCE({_q(r['purchase_type'])}, '{_PT_NONE}')"
        attach_new = "FALSE"
    else:
        pt_o = pt_r = f"'{_PT_ALL}'"
        attach_new = "TRUE"

    sql = f"""
WITH o AS (
    SELECT {period_expr(params.grain, o_date)} AS period,
           {_q(o["bucket"])} AS bucket,
           {pt_o} AS purchase_type,
           COUNT(DISTINCT IF({paid}, {_q(o["order_id"])}, NULL)) AS orders,
           COUNT(DISTINCT IF({paid}, {_q(o["customer_id"])}, NULL)) AS customers,
           SUM(IF({paid} AND {prod}, {_q(o["units"])}, 0)) AS units,
           SUM(IF({paid}, {_q(o["gross"])}, 0.0)) AS gross_sales,
           SUM(IF({paid}, {_q(o["net"])}, 0.0)) AS net_line_sales,
           COUNT(DISTINCT IF(NOT {paid}, {_q(o["order_id"])}, NULL)) AS unpaid_orders,
           SUM(IF(NOT {paid}, {_q(o["gross"])}, 0.0)) AS unpaid_list_value
    FROM dtc_order_lines
    WHERE {_date_pred(o_date, start, end)}
    GROUP BY period, bucket, purchase_type
),
r AS (
    SELECT {period_expr(params.grain, r_date)} AS period,
           {_q(r["bucket"])} AS bucket,
           {pt_r} AS purchase_type,
           SUM({_q(r["gross"])}) AS gross_revenue,
           SUM({_q(r["refund"])}) AS refunds_allocated,
           SUM({_q(r["net"])}) AS admin_net_revenue
    FROM dtc_revenue_lines
    WHERE {_date_pred(r_date, start, end)}
    GROUP BY period, bucket, purchase_type
),
n AS (
    SELECT {period_expr(params.grain, n_date)} AS period,
           COUNT(DISTINCT {_q(n["customer_id"])}) AS new_customers
    FROM dtc_customer_first_order
    WHERE {_date_pred(n_date, start, end)}
    GROUP BY period
),
-- FULL JOIN ... USING keeps a bucket that has revenue-view rows but no
-- order lines (or vice versa) instead of dropping it; USING coalesces keys.
joined AS (
    SELECT period, bucket, purchase_type,
           orders, customers, units, gross_sales, net_line_sales,
           unpaid_orders, unpaid_list_value,
           gross_revenue, refunds_allocated, admin_net_revenue
    FROM o FULL JOIN r USING (period, bucket, purchase_type)
)
SELECT j.period, j.bucket, j.purchase_type,
       COALESCE(j.orders, 0) AS orders,
       COALESCE(j.customers, 0) AS customers,
       IF(j.bucket = '{CORE_BUCKET}' AND {attach_new}, COALESCE(n.new_customers, 0), NULL)
           AS new_customers,
       COALESCE(j.units, 0) AS units,
       COALESCE(j.gross_sales, 0.0) AS gross_sales,
       COALESCE(j.net_line_sales, 0.0) AS net_line_sales,
       SAFE_DIVIDE(j.gross_sales, j.orders) AS aov,
       COALESCE(j.gross_revenue, 0.0) AS gross_revenue,
       COALESCE(j.refunds_allocated, 0.0) AS refunds_allocated,
       COALESCE(j.admin_net_revenue, 0.0) AS admin_net_revenue,
       COALESCE(j.unpaid_orders, 0) AS unpaid_orders,
       COALESCE(j.unpaid_list_value, 0.0) AS unpaid_list_value
FROM joined j
LEFT JOIN n ON n.period = j.period
ORDER BY j.period NULLS LAST, j.bucket, j.purchase_type
LIMIT {ROW_CAP}
"""
    rows, err = _execute(warehouse, sql, fmt)
    if err is not None or rows is None:
        return err  # type: ignore[return-value]

    today = today_reporting()
    _stamp_periods(rows, params.grain, start, end, today)

    selected = set(params.buckets)
    kept: list[dict[str, Any]] = []
    other: dict[str, dict[str, float]] = {}
    for row in rows:
        b = str(row.get("bucket"))
        if b in selected:
            if not params.by_purchase_type:
                row.pop("purchase_type", None)
            kept.append(row)
            continue
        acc = other.setdefault(b, dict.fromkeys(_OTHER_BUCKET_MEASURES, 0.0))
        for m in _OTHER_BUCKET_MEASURES:
            acc[m] += _f(row.get(m))
    for acc in other.values():
        acc["orders"] = int(acc["orders"])
        acc["unpaid_orders"] = int(acc["unpaid_orders"])

    unknown_orders = 0
    if UNKNOWN_BUCKET in other:
        unknown_orders = int(
            other[UNKNOWN_BUCKET]["orders"] + other[UNKNOWN_BUCKET]["unpaid_orders"]
        )
    else:
        unknown_orders = sum(
            int(_f(r.get("orders")) + _f(r.get("unpaid_orders")))
            for r in kept
            if r.get("bucket") == UNKNOWN_BUCKET
        )

    columns = list(_DTC_SALES_MARKDOWN_COLUMNS)
    if params.by_purchase_type:
        columns.insert(2, "purchase_type")

    extra: dict[str, Any] = {
        **_window_extra(params, start, end),
        "buckets": list(params.buckets),
        "by_purchase_type": params.by_purchase_type,
        "bucket_definitions": {
            **{k: list(v) for k, v in DTC_SOURCE_BUCKETS.items()},
            UNKNOWN_BUCKET: ["anything else, including NULL (a 2026-06-18..29 load gap)"],
        },
        "other_buckets": other,
        "other_buckets_note": (
            "window totals of the buckets not selected — reported, never dropped. "
            "gifting's unpaid_list_value is $0 orders priced at list; it is not sales."
        ),
        "unknown_source_orders": unknown_orders,
        "unknown_source_note": (
            "orders whose order_source is not in DTC_SOURCE_BUCKETS (or is NULL) in this "
            "window; run scripts/phase0/03_order_sources_recent.sql and add the source to "
            "bq.DTC_SOURCE_BUCKETS before trusting D2C scope"
            if unknown_orders
            else None
        ),
        "definitions": _DTC_SALES_DEFINITIONS,
        "resolved_columns": _names(
            {"dtc_order_lines": o, "dtc_revenue_lines": r, "dtc_customer_first_order": n}
        ),
        "sql": sql,
    }
    title = (
        f"DTC sales summary ({params.grain}, {start}..{end}, buckets={','.join(params.buckets)})"
    )
    return make_table_response(rows=kept, columns=columns, title=title, extra=extra, fmt=fmt)


# --------------------------------------------------------------------------------------
# bpd_get_ads_performance
# --------------------------------------------------------------------------------------

_ADS_DEFINITIONS = {
    "spend": "SUM(spend) over ads_spend_daily — Meta ad-level fact + Google CAMPAIGN fact only",
    "ctr": "SAFE_DIVIDE(clicks, impressions)",
    "cpc": "SAFE_DIVIDE(spend, clicks)",
    "cpm": "SAFE_DIVIDE(spend * 1000, impressions)",
    "cpa": "SAFE_DIVIDE(spend, conversions) — platform-attributed conversions",
    "platform_roas": (
        "SAFE_DIVIDE(conversion_value, spend) — the PLATFORM's attributed value "
        "(Meta purchase_value, Google conversions_value), not warehouse revenue; "
        "compare with bpd_get_marketing_efficiency"
    ),
    "days_with_spend_rows": "distinct dates with a fact row in the period",
    "days_delivered": "media_delivery_status = DELIVERED",
    "days_confirmed_zero": "OBSERVED_ZERO or CONFIRMED_NO_DELIVERY — a real zero",
    "days_undiagnosed": "ABSENT_UNDIAGNOSED — a pipeline gap, never a zero",
    "days_unclassified": "calendar days in the window the view has no row for (today, or before the channel's history starts)",
    "delivery_flag": "undiagnosed_gap > unclassified_days > ok",
    "restatement_note": (
        "platforms restate recent days (Meta attribution window up to 28 days); "
        "a period's spend/conversions can move after it first lands"
    ),
}

_ADS_MARKDOWN_COLUMNS = [
    "period",
    "channel",
    "spend",
    "clicks",
    "conversions",
    "conversion_value",
    "platform_roas",
    "delivery_flag",
    "partial_period",
]

_ADS_CAMPAIGN_MARKDOWN_COLUMNS = [
    "period",
    "channel",
    "campaign_name",
    "campaign_status",
    "spend",
    "clicks",
    "conversions",
    "platform_roas",
    "partial_period",
]

_DELIVERY_ZERO_FIELDS = (
    "days_delivered",
    "days_confirmed_zero",
    "days_undiagnosed",
    "days_classified",
)


def _delivery_flag(row: Mapping[str, Any]) -> str:
    if _f(row.get("days_undiagnosed")) > 0:
        return "undiagnosed_gap"
    if _f(row.get("days_unclassified")) > 0:
        return "unclassified_days"
    return "ok"


def _finish_delivery_rows(rows: list[dict[str, Any]], grain: str, start: date, end: date) -> None:
    for d in rows:
        p = d.get("period")
        n_days = days_in_window(grain, p, start, end) if isinstance(p, date) else 0
        d["days_in_window"] = n_days
        d["days_unclassified"] = max(n_days - int(_f(d.get("days_classified"))), 0)
        d["delivery_flag"] = _delivery_flag(d)


async def get_ads_performance(warehouse: Warehouse, params: AdsPerformanceInput) -> ToolResponse:
    fmt = params.response_format
    start, end = resolve_window(params.start_date, params.end_date)
    if err := _window_error(start, end, fmt):
        return err
    if err := _require_tables(warehouse, ("ads_spend_daily", "media_delivery_status"), fmt):
        return err
    campaigns_present = table_exists(warehouse, "ads_campaigns")
    try:
        s = _cols(
            warehouse,
            "ads_spend_daily",
            (
                "date",
                "channel",
                "campaign",
                "spend",
                "impressions",
                "clicks",
                "conversions",
                "conversion_value",
            ),
        )
        d = _cols(warehouse, "media_delivery_status", ("date", "channel", "status"))
        c = (
            _cols(warehouse, "ads_campaigns", ("channel", "campaign", "name", "status"))
            if campaigns_present
            else {}
        )
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    s_date, d_date = s["date"].select_as_date(), d["date"].select_as_date()
    chan_pred_s = f" AND {_q(s['channel'])} = '{params.channel}'" if params.channel != "all" else ""
    chan_pred_d = f" AND {_q(d['channel'])} = '{params.channel}'" if params.channel != "all" else ""

    by_campaign = params.by_campaign
    camp_sel = f",\n           {_q(s['campaign'])} AS campaign_id" if by_campaign else ""
    camp_key = ", campaign_id" if by_campaign else ""
    top_cte = (
        f""",
top AS (
    SELECT channel, campaign_id
    FROM s
    GROUP BY channel, campaign_id
    ORDER BY SUM(spend) DESC
    LIMIT {int(params.top_n)}
)"""
        if by_campaign
        else ""
    )
    names_cte = (
        f""",
c AS (
    SELECT {_q(c["channel"])} AS channel, {_q(c["campaign"])} AS campaign_id,
           ANY_VALUE({_q(c["name"])}) AS campaign_name,
           ANY_VALUE({_q(c["status"])}) AS campaign_status
    FROM ads_campaigns
    GROUP BY channel, campaign_id
)"""
        if by_campaign and campaigns_present
        else ""
    )
    if by_campaign:
        name_cols = (
            ", s.campaign_id, c.campaign_name, c.campaign_status"
            if campaigns_present
            else ", s.campaign_id, CAST(NULL AS STRING) AS campaign_name, "
            "CAST(NULL AS STRING) AS campaign_status"
        )
        joins = "JOIN top USING (channel, campaign_id)" + (
            "\nLEFT JOIN c USING (channel, campaign_id)" if campaigns_present else ""
        )
        order = "ORDER BY s.period NULLS LAST, s.channel, s.spend DESC"
    else:
        name_cols, joins = "", ""
        order = "ORDER BY s.period NULLS LAST, s.channel"

    perf_sql = f"""
WITH s AS (
    SELECT {period_expr(params.grain, s_date)} AS period,
           {_q(s["channel"])} AS channel{camp_sel},
           SUM({_q(s["spend"])}) AS spend,
           SUM({_q(s["impressions"])}) AS impressions,
           SUM({_q(s["clicks"])}) AS clicks,
           SUM({_q(s["conversions"])}) AS conversions,
           SUM({_q(s["conversion_value"])}) AS conversion_value,
           COUNT(DISTINCT {s_date}) AS days_with_spend_rows
    FROM ads_spend_daily
    WHERE {_date_pred(s_date, start, end)}{chan_pred_s}
    GROUP BY period, channel{camp_key}
){top_cte}{names_cte}
SELECT s.period, s.channel{name_cols},
       s.spend, s.impressions, s.clicks, s.conversions, s.conversion_value,
       s.days_with_spend_rows,
       SAFE_DIVIDE(s.clicks, s.impressions) AS ctr,
       SAFE_DIVIDE(s.spend, s.clicks) AS cpc,
       SAFE_DIVIDE(s.spend * 1000, s.impressions) AS cpm,
       SAFE_DIVIDE(s.spend, s.conversions) AS cpa,
       SAFE_DIVIDE(s.conversion_value, s.spend) AS platform_roas
FROM s
{joins}
{order}
LIMIT {ROW_CAP}
"""
    delivery_sql = f"""
SELECT {period_expr(params.grain, d_date)} AS period,
       {_q(d["channel"])} AS channel,
       COUNTIF({_q(d["status"])} = '{DELIVERY_DELIVERED}') AS days_delivered,
       COUNTIF({_q(d["status"])} IN ({", ".join(repr(x) for x in DELIVERY_ZERO)})) AS days_confirmed_zero,
       COUNTIF({_q(d["status"])} = '{DELIVERY_UNDIAGNOSED}') AS days_undiagnosed,
       COUNT(*) AS days_classified
FROM media_delivery_status
WHERE {_date_pred(d_date, start, end)}{chan_pred_d}
GROUP BY period, channel
ORDER BY period NULLS LAST, channel
LIMIT {ROW_CAP}
"""
    perf, err = _execute(warehouse, perf_sql, fmt)
    if err is not None or perf is None:
        return err  # type: ignore[return-value]
    delivery, err = _execute(warehouse, delivery_sql, fmt)
    if err is not None or delivery is None:
        return err  # type: ignore[return-value]

    today = today_reporting()
    _stamp_periods(perf, params.grain, start, end, today)
    _stamp_periods(delivery, params.grain, start, end, today)
    _finish_delivery_rows(delivery, params.grain, start, end)

    if by_campaign:
        rows = perf
        columns = list(_ADS_CAMPAIGN_MARKDOWN_COLUMNS)
    else:
        # Merge on (period, channel). A delivery-only key means the view says
        # the channel was live but the fact has no rows — worth a row of its
        # own with NULL measures, not a silent drop.
        by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
        for p in perf:
            by_key[(p["period"], p["channel"])] = p
        for dl in delivery:
            key = (dl["period"], dl["channel"])
            row = by_key.setdefault(
                key,
                {
                    "period": dl["period"],
                    "channel": dl["channel"],
                    "spend": None,
                    "impressions": None,
                    "clicks": None,
                    "conversions": None,
                    "conversion_value": None,
                    "days_with_spend_rows": 0,
                    "ctr": None,
                    "cpc": None,
                    "cpm": None,
                    "cpa": None,
                    "platform_roas": None,
                    "partial_period": dl["partial_period"],
                },
            )
            for f_ in (
                *_DELIVERY_ZERO_FIELDS,
                "days_in_window",
                "days_unclassified",
                "delivery_flag",
            ):
                row[f_] = dl[f_]
        for row in by_key.values():
            if "days_classified" not in row:
                p = row["period"]
                n_days = days_in_window(params.grain, p, start, end) if isinstance(p, date) else 0
                row.update(dict.fromkeys(_DELIVERY_ZERO_FIELDS, 0))
                row["days_in_window"] = n_days
                row["days_unclassified"] = n_days
                row["delivery_flag"] = _delivery_flag(row)
        rows = sorted(
            by_key.values(),
            key=lambda r: (r["period"] is None, r["period"] or date.min, str(r["channel"])),
        )
        columns = list(_ADS_MARKDOWN_COLUMNS)

    flagged = [
        {
            "period": str(dl["period"]),
            "channel": dl["channel"],
            "flag": dl["delivery_flag"],
            "days_undiagnosed": dl["days_undiagnosed"],
            "days_unclassified": dl["days_unclassified"],
        }
        for dl in delivery
        if dl["delivery_flag"] != "ok"
    ]
    integrity = {
        "undiagnosed_days": int(sum(_f(dl["days_undiagnosed"]) for dl in delivery)),
        "unclassified_days": int(sum(_f(dl["days_unclassified"]) for dl in delivery)),
        "flagged_periods": flagged[:50],
        "note": (
            "an undiagnosed day is a pipeline gap: the period's spend is a LOWER bound. "
            "Unclassified days are usually today (one-day platform lag) or dates before "
            f"a channel's history starts (Meta: {META_HISTORY_START})."
        ),
    }
    extra: dict[str, Any] = {
        **_window_extra(params, start, end),
        "channel": params.channel,
        "by_campaign": by_campaign,
        "top_n": params.top_n if by_campaign else None,
        "campaign_names_available": campaigns_present,
        "integrity": integrity,
        "definitions": _ADS_DEFINITIONS,
        "resolved_columns": _names(
            {
                "ads_spend_daily": s,
                "media_delivery_status": d,
                **({"ads_campaigns": c} if c else {}),
            }
        ),
        "sql": perf_sql,
        "delivery_sql": delivery_sql,
    }
    if by_campaign:
        extra["delivery"] = delivery
    title = (
        f"Ads performance ({params.grain}, {start}..{end}, channel={params.channel}"
        + (f", top {params.top_n} campaigns" if by_campaign else "")
        + ")"
    )
    return make_table_response(rows=rows, columns=columns, title=title, extra=extra, fmt=fmt)


# --------------------------------------------------------------------------------------
# bpd_get_marketing_efficiency
# --------------------------------------------------------------------------------------

_EFFICIENCY_DEFINITIONS = {
    "spend": "all-channel SUM(spend) from ads_spend_daily (meta_spend + google_spend)",
    "orders / customers / gross_sales / net_line_sales": (
        "core_d2c PAID orders only (channel_bucket = 'core_d2c' AND is_paid_order)"
    ),
    "admin_net_revenue": "core_d2c rows of dtc_revenue_lines (certified view), order-date basis",
    "new_customers": "rows of dtc_customer_first_order in the period (first PAID core-D2C order)",
    "mer_gross": "SAFE_DIVIDE(gross_sales, spend) — marketing efficiency ratio on gross",
    "mer_net": "SAFE_DIVIDE(admin_net_revenue, spend)",
    "blended_cac": "SAFE_DIVIDE(spend, new_customers) — all spend over new customers",
    "cost_per_order": "SAFE_DIVIDE(spend, orders)",
    "new_customer_share": "SAFE_DIVIDE(new_customers, orders)",
    "platform_roas": (
        "SAFE_DIVIDE(platform_conversion_value, spend) — the platforms' own attribution; "
        "expect it to differ from mer_* and read the gap as attribution, not error"
    ),
    "channels_reporting": (
        "both | google_only | meta_only | none — from which channels had spend rows. "
        f"Meta history starts {META_HISTORY_START}; earlier periods are google_only by construction"
    ),
}

_EFFICIENCY_MARKDOWN_COLUMNS = [
    "period",
    "spend",
    "orders",
    "new_customers",
    "gross_sales",
    "admin_net_revenue",
    "mer_gross",
    "blended_cac",
    "platform_roas",
    "channels_reporting",
    "partial_period",
]


def _channels_reporting(meta_spend: Any, google_spend: Any) -> str:
    m, g = _f(meta_spend) > 0, _f(google_spend) > 0
    if m and g:
        return "both"
    if g:
        return "google_only"
    if m:
        return "meta_only"
    return "none"


async def get_marketing_efficiency(
    warehouse: Warehouse, params: MarketingEfficiencyInput
) -> ToolResponse:
    fmt = params.response_format
    start, end = resolve_window(params.start_date, params.end_date)
    if err := _window_error(start, end, fmt):
        return err
    if err := _require_tables(
        warehouse,
        ("dtc_order_lines", "dtc_revenue_lines", "dtc_customer_first_order", "ads_spend_daily"),
        fmt,
    ):
        return err
    try:
        o = _cols(
            warehouse,
            "dtc_order_lines",
            ("date", "order_id", "customer_id", "bucket", "paid", "gross", "net"),
        )
        r = _cols(warehouse, "dtc_revenue_lines", ("date", "bucket", "net"))
        n = _cols(warehouse, "dtc_customer_first_order", ("date", "customer_id"))
        s = _cols(
            warehouse,
            "ads_spend_daily",
            ("date", "channel", "spend", "conversions", "conversion_value"),
        )
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    o_date, r_date = o["date"].select_as_date(), r["date"].select_as_date()
    n_date, s_date = n["date"].select_as_date(), s["date"].select_as_date()
    g = params.grain

    sql = f"""
WITH sales AS (
    SELECT {period_expr(g, o_date)} AS period,
           COUNT(DISTINCT {_q(o["order_id"])}) AS orders,
           COUNT(DISTINCT {_q(o["customer_id"])}) AS customers,
           SUM({_q(o["gross"])}) AS gross_sales,
           SUM({_q(o["net"])}) AS net_line_sales
    FROM dtc_order_lines
    WHERE {_date_pred(o_date, start, end)}
      AND {_q(o["bucket"])} = '{CORE_BUCKET}' AND {_q(o["paid"])}
    GROUP BY period
),
rev AS (
    SELECT {period_expr(g, r_date)} AS period,
           SUM({_q(r["net"])}) AS admin_net_revenue
    FROM dtc_revenue_lines
    WHERE {_date_pred(r_date, start, end)} AND {_q(r["bucket"])} = '{CORE_BUCKET}'
    GROUP BY period
),
newc AS (
    SELECT {period_expr(g, n_date)} AS period,
           COUNT(DISTINCT {_q(n["customer_id"])}) AS new_customers
    FROM dtc_customer_first_order
    WHERE {_date_pred(n_date, start, end)}
    GROUP BY period
),
spend AS (
    SELECT {period_expr(g, s_date)} AS period,
           SUM({_q(s["spend"])}) AS spend,
           SUM(IF({_q(s["channel"])} = 'meta', {_q(s["spend"])}, 0.0)) AS meta_spend,
           SUM(IF({_q(s["channel"])} = 'google', {_q(s["spend"])}, 0.0)) AS google_spend,
           SUM({_q(s["conversions"])}) AS platform_conversions,
           SUM({_q(s["conversion_value"])}) AS platform_conversion_value
    FROM ads_spend_daily
    WHERE {_date_pred(s_date, start, end)}
    GROUP BY period
),
-- An explicit period spine, so a period with spend and no sales (or the
-- reverse) is a row with zeros on the empty side rather than a dropped row.
-- Numerators are COALESCEd to 0 so "spend, no sales" reads MER 0, not NULL;
-- denominators are left alone so "no spend" / "no new customers" reads NULL.
spine AS (
    SELECT period FROM sales UNION DISTINCT
    SELECT period FROM rev UNION DISTINCT
    SELECT period FROM newc UNION DISTINCT
    SELECT period FROM spend
)
SELECT k.period,
       COALESCE(spend.spend, 0.0) AS spend,
       COALESCE(spend.meta_spend, 0.0) AS meta_spend,
       COALESCE(spend.google_spend, 0.0) AS google_spend,
       COALESCE(sales.orders, 0) AS orders,
       COALESCE(sales.customers, 0) AS customers,
       COALESCE(newc.new_customers, 0) AS new_customers,
       COALESCE(sales.gross_sales, 0.0) AS gross_sales,
       COALESCE(sales.net_line_sales, 0.0) AS net_line_sales,
       COALESCE(rev.admin_net_revenue, 0.0) AS admin_net_revenue,
       SAFE_DIVIDE(COALESCE(sales.gross_sales, 0.0), spend.spend) AS mer_gross,
       SAFE_DIVIDE(COALESCE(rev.admin_net_revenue, 0.0), spend.spend) AS mer_net,
       SAFE_DIVIDE(spend.spend, newc.new_customers) AS blended_cac,
       SAFE_DIVIDE(spend.spend, sales.orders) AS cost_per_order,
       SAFE_DIVIDE(COALESCE(newc.new_customers, 0), sales.orders) AS new_customer_share,
       COALESCE(spend.platform_conversions, 0.0) AS platform_conversions,
       COALESCE(spend.platform_conversion_value, 0.0) AS platform_conversion_value,
       SAFE_DIVIDE(COALESCE(spend.platform_conversion_value, 0.0), spend.spend) AS platform_roas
FROM spine k
LEFT JOIN sales USING (period)
LEFT JOIN rev USING (period)
LEFT JOIN newc USING (period)
LEFT JOIN spend USING (period)
ORDER BY k.period NULLS LAST
LIMIT {ROW_CAP}
"""
    rows, err = _execute(warehouse, sql, fmt)
    if err is not None or rows is None:
        return err  # type: ignore[return-value]

    today = today_reporting()
    _stamp_periods(rows, g, start, end, today)
    for row in rows:
        row["channels_reporting"] = _channels_reporting(
            row.get("meta_spend"), row.get("google_spend")
        )

    pre_meta = [
        str(r["period"])
        for r in rows
        if isinstance(r["period"], date) and r["period"] < META_HISTORY_START
    ]
    extra: dict[str, Any] = {
        **_window_extra(params, start, end),
        "scope": f"{CORE_BUCKET} paid orders vs all-channel spend",
        "periods_before_meta_history": pre_meta[:50],
        "definitions": _EFFICIENCY_DEFINITIONS,
        "resolved_columns": _names(
            {
                "dtc_order_lines": o,
                "dtc_revenue_lines": r,
                "dtc_customer_first_order": n,
                "ads_spend_daily": s,
            }
        ),
        "sql": sql,
    }
    title = f"Marketing efficiency ({g}, {start}..{end}, core_d2c vs all-channel spend)"
    return make_table_response(
        rows=rows, columns=list(_EFFICIENCY_MARKDOWN_COLUMNS), title=title, extra=extra, fmt=fmt
    )


# --------------------------------------------------------------------------------------
# bpd_get_dtc_pacing
# --------------------------------------------------------------------------------------
#
# Month-to-date pacing of core-D2C demand, spend and new customers against three
# yardsticks: the ecomm team's daily forecast (config/dtc_pacing_targets.json,
# regenerated from their pacing sheet — see pacing_targets.py), the equivalent
# span immediately before the month and the same day-of-month span last month
# (period over period), and the weekday-aligned span a year earlier.
#
# "Demand" here is deliberately the SHEET's definition, not gross_sales: the
# order-level subtotal after discounts plus shipping, i.e. Shopify's total less
# tax, over paid core-D2C orders reduced to one row per order. Reconciled
# against the sheet's Actual DMD for 2026-09-01..09 it lands within 0-1.3% on
# seven of nine days and within 7% on the other two (the sheet's pull runs on a
# different clock). The remaining gap is reported per day as
# `sheet_vs_warehouse_pct` wherever the config carries the sheet's own actual.

#: 52 weeks, so "last year" compares Tuesday with Tuesday, not the 1st with the 1st.
LY_SHIFT_DAYS = 364


@dataclass(frozen=True)
class PacingWindows:
    as_of: date
    month: str
    month_start: date
    month_end: date
    days_in_month: int
    mtd_end: date
    """The last COMPLETE day being paced."""
    elapsed: int
    days_left: int
    prior_start: date
    prior_end: date
    """The `elapsed` days immediately before the month (trend)."""
    prior_month_start: date
    prior_month_end: date
    """The same day-of-month span in the previous month, clipped to its length (MoM)."""
    ly_mtd_start: date
    ly_mtd_end: date
    ly_month_start: date
    ly_month_end: date

    @property
    def query_start(self) -> date:
        return min(self.prior_start, self.prior_month_start, self.ly_month_start)

    @property
    def query_end(self) -> date:
        return max(self.mtd_end, self.ly_month_end)


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    return first, date(y, m, calendar.monthrange(y, m)[1])


def resolve_pacing_windows(as_of: date | None, month: str | None, today: date) -> PacingWindows:
    """Turn (as_of, month) into every window the tool needs. Raises ValueError when
    the month has no complete day on or before as_of."""
    as_of_d = as_of or today
    complete_through = as_of_d - timedelta(days=1) if as_of_d >= today else as_of_d
    ym = month or complete_through.strftime("%Y-%m")
    month_start, month_end = month_bounds(ym)
    mtd_end = min(complete_through, month_end)
    if mtd_end < month_start:
        raise ValueError(
            f"{ym} has no complete day on or before {complete_through} (as_of {as_of_d})"
        )
    days_in_month = (month_end - month_start).days + 1
    elapsed = (mtd_end - month_start).days + 1
    prior_end = month_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=elapsed - 1)
    pm_start, pm_last = month_bounds(prior_end.strftime("%Y-%m"))
    pm_end = min(pm_start + timedelta(days=elapsed - 1), pm_last)
    shift = timedelta(days=LY_SHIFT_DAYS)
    return PacingWindows(
        as_of=as_of_d,
        month=ym,
        month_start=month_start,
        month_end=month_end,
        days_in_month=days_in_month,
        mtd_end=mtd_end,
        elapsed=elapsed,
        days_left=days_in_month - elapsed,
        prior_start=prior_start,
        prior_end=prior_end,
        prior_month_start=pm_start,
        prior_month_end=pm_end,
        ly_mtd_start=month_start - shift,
        ly_mtd_end=mtd_end - shift,
        ly_month_start=month_start - shift,
        ly_month_end=month_end - shift,
    )


_DAILY_MEASURES: tuple[str, ...] = (
    "demand",
    "net_sales",
    "shipping",
    "orders",
    "new_customers",
    "spend",
    "platform_conversion_value",
)


def _pct_change(actual: float | None, base: float | None) -> float | None:
    """(actual - base) / base as a true percent; None when base is 0/None."""
    if actual is None or base is None or base == 0:
        return None
    return round((actual - base) / base * 100.0, 2)


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _window_totals(
    daily: Mapping[date, Mapping[str, Any]], start: date, end: date
) -> dict[str, Any]:
    """Sum the daily measures over [start, end], missing days counting as zero, plus ratios."""
    tot: dict[str, Any] = dict.fromkeys(_DAILY_MEASURES, 0.0)
    d = start
    while d <= end:
        row = daily.get(d)
        if row:
            for m in _DAILY_MEASURES:
                tot[m] += _f(row.get(m))
        d += timedelta(days=1)
    for m in ("demand", "net_sales", "shipping", "spend", "platform_conversion_value"):
        tot[m] = round(tot[m], 2)
    tot["orders"] = int(tot["orders"])
    tot["new_customers"] = int(tot["new_customers"])
    tot["days"] = (end - start).days + 1
    tot["window"] = {"start": str(start), "end": str(end)}
    tot["aov"] = _ratio(tot["demand"], tot["orders"])
    tot["mer"] = _ratio(tot["demand"], tot["spend"])
    tot["cac"] = _ratio(tot["spend"], tot["new_customers"])
    tot["platform_roas"] = _ratio(tot["platform_conversion_value"], tot["spend"])
    return tot


def _compare(current: Mapping[str, Any], base: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for m in ("demand", "orders", "new_customers", "spend"):
        out[f"{m}_change_pct"] = _pct_change(current[m], base[m])
    return out


def _money(v: Any) -> str:
    return "n/a" if v is None else f"${v:,.0f}"


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _num(v: Any, nd: int = 0) -> str:
    return "n/a" if v is None else f"{v:,.{nd}f}"


_PACING_DAILY_MARKDOWN = [
    "day",
    "dow",
    "demand",
    "forecast_demand",
    "act_vs_fcst_pct",
    "ly_demand",
    "act_vs_ly_pct",
    "spend",
    "new_customers",
]

_PACING_WEEKLY_MARKDOWN = [
    "week",
    "week_start",
    "days",
    "demand",
    "forecast_demand",
    "act_vs_fcst_pct",
    "ly_demand",
    "act_vs_ly_pct",
    "spend",
    "new_customers",
    "partial",
]

_PACING_DEFINITIONS = {
    "demand": (
        "SUM over PAID core-D2C orders (one row per order_id) of order_subtotal + "
        "order_shipping — Shopify total less tax; the ecomm sheet's 'Actual DMD'"
    ),
    "net_sales": "the same orders' order_subtotal (after discounts, before shipping and tax)",
    "orders": "COUNT of those orders",
    "new_customers": "rows of dtc_customer_first_order (first PAID core-D2C order)",
    "spend": "all-channel SUM(spend) from ads_spend_daily",
    "aov / mer / cac / platform_roas": "demand/orders, demand/spend, spend/new_customers, platform value/spend",
    "forecast_*": "the ecomm team's daily targets from config/dtc_pacing_targets.json (see extra.targets)",
    "act_vs_fcst_pct / act_vs_ly_pct / *_change_pct": "(actual - base) / base * 100",
    "prior_period": "the same number of elapsed days immediately before the month started",
    "prior_month_to_date": "the same day-of-month span in the previous month, clipped to its length",
    "last_year": f"the span {LY_SHIFT_DAYS} days earlier, so weekdays align",
    "run_rate_projection": "MTD actual / elapsed days * days in month",
    "required_daily_average": "(month forecast - MTD actual) / days left",
    "weeks": "Monday-anchored, numbered within the month; the first and last are usually partial",
    "sheet_actual_demand": "the sheet's own Actual DMD as of the config refresh — reconciliation only, never the source of a number here",
}


async def get_dtc_pacing(
    warehouse: Warehouse,
    params: DtcPacingInput,
    *,
    targets: PacingTargets | None = None,
) -> ToolResponse:
    fmt = params.response_format
    today = today_reporting()
    try:
        w = resolve_pacing_windows(params.as_of, params.month, today)
    except ValueError as e:
        return make_error_response(
            code="INVALID_DATE_RANGE",
            message=str(e),
            details={"as_of": str(params.as_of), "month": params.month, "today": str(today)},
            fmt=fmt,
        )
    if err := _require_tables(
        warehouse, ("dtc_order_lines", "dtc_customer_first_order", "ads_spend_daily"), fmt
    ):
        return err
    try:
        o = _cols(
            warehouse,
            "dtc_order_lines",
            ("date", "order_id", "bucket", "paid", "order_subtotal", "order_shipping"),
        )
        n = _cols(warehouse, "dtc_customer_first_order", ("date", "customer_id"))
        s = _cols(warehouse, "ads_spend_daily", ("date", "spend", "conversion_value"))
    except ColumnNotFound as e:
        return _column_not_found_error(e, fmt=fmt)

    # ---- targets: never fatal. Actuals and the period comparisons stand on their own.
    targets_status = "ok"
    targets_note: str | None = None
    month_targets = None
    tgt = targets
    if tgt is None:
        try:
            tgt = load_targets()
        except TargetsUnavailable as e:
            targets_status, targets_note = "unavailable", str(e)
    if tgt is not None:
        month_targets = tgt.month(w.month)
        if month_targets is None:
            targets_status = "missing_month"
            targets_note = (
                f"config/dtc_pacing_targets.json has no {w.month} tab (available: "
                f"{', '.join(tgt.available_months) or 'none'}). The sheet gets a new tab "
                f"each month — export it and run `{REFRESH_COMMAND}`."
            )

    o_date, n_date, s_date = (
        o["date"].select_as_date(),
        n["date"].select_as_date(),
        s["date"].select_as_date(),
    )
    lo, hi = w.query_start, w.query_end
    sql = f"""
WITH o AS (
    SELECT {o_date} AS d, {_q(o["order_id"])} AS order_id,
           ANY_VALUE({_q(o["order_subtotal"])}) AS order_subtotal,
           ANY_VALUE({_q(o["order_shipping"])}) AS order_shipping
    FROM dtc_order_lines
    WHERE {_date_pred(o_date, lo, hi)}
      AND {_q(o["bucket"])} = '{CORE_BUCKET}' AND {_q(o["paid"])}
    GROUP BY d, order_id
),
sales AS (
    SELECT d, COUNT(*) AS orders,
           SUM(COALESCE(order_subtotal, 0.0) + COALESCE(order_shipping, 0.0)) AS demand,
           SUM(COALESCE(order_subtotal, 0.0)) AS net_sales,
           SUM(COALESCE(order_shipping, 0.0)) AS shipping
    FROM o
    GROUP BY d
),
newc AS (
    SELECT {n_date} AS d, COUNT(DISTINCT {_q(n["customer_id"])}) AS new_customers
    FROM dtc_customer_first_order
    WHERE {_date_pred(n_date, lo, hi)}
    GROUP BY d
),
spend AS (
    SELECT {s_date} AS d, SUM({_q(s["spend"])}) AS spend,
           SUM({_q(s["conversion_value"])}) AS platform_conversion_value
    FROM ads_spend_daily
    WHERE {_date_pred(s_date, lo, hi)}
    GROUP BY d
),
spine AS (
    SELECT d FROM sales UNION DISTINCT
    SELECT d FROM newc UNION DISTINCT
    SELECT d FROM spend
)
SELECT k.d AS day,
       COALESCE(sales.orders, 0) AS orders,
       COALESCE(sales.demand, 0.0) AS demand,
       COALESCE(sales.net_sales, 0.0) AS net_sales,
       COALESCE(sales.shipping, 0.0) AS shipping,
       COALESCE(newc.new_customers, 0) AS new_customers,
       COALESCE(spend.spend, 0.0) AS spend,
       COALESCE(spend.platform_conversion_value, 0.0) AS platform_conversion_value
FROM spine k
LEFT JOIN sales USING (d)
LEFT JOIN newc USING (d)
LEFT JOIN spend USING (d)
ORDER BY k.d
LIMIT 5000
"""
    rows, err = _execute(warehouse, sql, fmt)
    if err is not None or rows is None:
        return err  # type: ignore[return-value]
    daily: dict[date, dict[str, Any]] = {}
    for r in rows:
        d = _as_pydate(r.get("day"))
        if d is not None:
            daily[d] = r

    # ---- windows
    mtd = _window_totals(daily, w.month_start, w.mtd_end)
    prior = _compare(mtd, _window_totals(daily, w.prior_start, w.prior_end))
    prior_month = _compare(mtd, _window_totals(daily, w.prior_month_start, w.prior_month_end))
    ly_mtd = _compare(mtd, _window_totals(daily, w.ly_mtd_start, w.ly_mtd_end))
    ly_month = _window_totals(daily, w.ly_month_start, w.ly_month_end)

    # ---- forecast (MTD and month) and everything derived from it
    fc_mtd: dict[str, Any] | None = None
    fc_month: dict[str, Any] | None = None
    variance: dict[str, Any] | None = None
    to_go: dict[str, Any] | None = None
    required_daily: dict[str, Any] | None = None
    run_rate: dict[str, Any] = {
        m: (mtd[m] / w.elapsed * w.days_in_month) if w.elapsed else None
        for m in ("demand", "spend", "new_customers")
    }
    projection_vs_forecast: dict[str, Any] | None = None
    if month_targets is not None:
        fc_mtd = {
            "demand": month_targets.series_sum("forecast_demand", w.month_start, w.mtd_end),
            "spend": month_targets.series_sum("forecast_spend", w.month_start, w.mtd_end),
            "new_customers": month_targets.series_sum(
                "forecast_new_customers", w.month_start, w.mtd_end
            ),
        }
        fc_mtd["mer"] = _ratio(fc_mtd["demand"], fc_mtd["spend"])
        fc_mtd["cac"] = _ratio(fc_mtd["spend"], fc_mtd["new_customers"])
        fc_month = {
            "demand": month_targets.month_total("forecast_demand"),
            "spend": month_targets.month_total("forecast_spend"),
            "new_customers": month_targets.month_total("forecast_new_customers"),
        }
        variance = {
            f"{m}_pct": _pct_change(mtd[m], fc_mtd[m]) for m in ("demand", "spend", "new_customers")
        }
        to_go = {
            m: (fc_month[m] - mtd[m]) if fc_month[m] is not None else None
            for m in ("demand", "spend", "new_customers")
        }
        required_daily = {
            m: (to_go[m] / w.days_left) if (to_go[m] is not None and w.days_left > 0) else None
            for m in ("demand", "spend", "new_customers")
        }
        projection_vs_forecast = {
            f"{m}_pct": _pct_change(run_rate[m], fc_month[m])
            for m in ("demand", "spend", "new_customers")
        }

    # ---- daily rows for the MTD span
    daily_rows: list[dict[str, Any]] = []
    d = w.month_start
    while d <= w.mtd_end:
        row = daily.get(d, {})
        ly_row = daily.get(d - timedelta(days=LY_SHIFT_DAYS), {})
        t = month_targets.days.get(d) if month_targets is not None else None
        fc_demand = t.get("forecast_demand") if t else None
        sheet_actual = t.get("sheet_actual_demand") if t else None
        demand = round(_f(row.get("demand")), 2)
        ly_demand = round(_f(ly_row.get("demand")), 2)
        daily_rows.append(
            {
                "day": d,
                "dow": d.strftime("%a"),
                "demand": demand,
                "net_sales": round(_f(row.get("net_sales")), 2),
                "shipping": round(_f(row.get("shipping")), 2),
                "orders": int(_f(row.get("orders"))),
                "new_customers": int(_f(row.get("new_customers"))),
                "spend": round(_f(row.get("spend")), 2),
                "forecast_demand": fc_demand,
                "forecast_spend": t.get("forecast_spend") if t else None,
                "forecast_new_customers": t.get("forecast_new_customers") if t else None,
                "act_vs_fcst_pct": _pct_change(demand, fc_demand),
                "ly_day": d - timedelta(days=LY_SHIFT_DAYS),
                "ly_demand": ly_demand,
                "act_vs_ly_pct": _pct_change(demand, ly_demand or None),
                "sheet_actual_demand": sheet_actual,
                "sheet_vs_warehouse_pct": _pct_change(sheet_actual, demand or None),
            }
        )
        d += timedelta(days=1)

    # ---- weekly rows: Monday-anchored weeks intersecting the month, through mtd_end
    weekly_rows: list[dict[str, Any]] = []
    wk_start = w.month_start - timedelta(days=w.month_start.weekday())
    n_week = 0
    while wk_start <= w.mtd_end:
        n_week += 1
        lo_d, hi_d = max(wk_start, w.month_start), min(wk_start + timedelta(days=6), w.mtd_end)
        tot = _window_totals(daily, lo_d, hi_d)
        ly = _window_totals(
            daily, lo_d - timedelta(days=LY_SHIFT_DAYS), hi_d - timedelta(days=LY_SHIFT_DAYS)
        )
        fc = month_targets.series_sum("forecast_demand", lo_d, hi_d) if month_targets else None
        weekly_rows.append(
            {
                "week": f"wk {n_week}",
                "week_start": wk_start,
                "days": tot["days"],
                "demand": tot["demand"],
                "forecast_demand": fc,
                "act_vs_fcst_pct": _pct_change(tot["demand"], fc),
                "ly_demand": ly["demand"],
                "act_vs_ly_pct": _pct_change(tot["demand"], ly["demand"] or None),
                "orders": tot["orders"],
                "spend": tot["spend"],
                "new_customers": tot["new_customers"],
                "partial": (wk_start + timedelta(days=6)) > min(w.mtd_end, w.month_end)
                or wk_start < w.month_start,
            }
        )
        wk_start += timedelta(days=7)

    summary: dict[str, Any] = {
        "month": w.month,
        "as_of": str(w.as_of),
        "complete_through": str(w.mtd_end),
        "elapsed_days": w.elapsed,
        "days_left": w.days_left,
        "days_in_month": w.days_in_month,
        "mtd": mtd,
        "forecast_mtd": fc_mtd,
        "variance_vs_forecast_mtd": variance,
        "month_forecast": fc_month,
        "to_go": to_go,
        "required_daily_average": required_daily,
        "run_rate_projection": run_rate,
        "projection_vs_forecast": projection_vs_forecast,
        "prior_period": prior,
        "prior_month_to_date": prior_month,
        "last_year_mtd": ly_mtd,
        "last_year_month": ly_month,
    }
    extra: dict[str, Any] = {
        "summary": summary,
        "weekly": weekly_rows,
        "targets": {
            "status": targets_status,
            "note": targets_note,
            "month_tab": month_targets.tab if month_targets else None,
            "source": tgt.source if tgt is not None else None,
            "path": str(tgt.path) if tgt is not None and tgt.path else None,
        },
        "definitions": _PACING_DEFINITIONS,
        "resolved_columns": _names(
            {"dtc_order_lines": o, "dtc_customer_first_order": n, "ads_spend_daily": s}
        ),
        "sql": sql,
    }
    if params.include_daily:
        extra["daily"] = daily_rows

    title = (
        f"DTC pacing — {w.month}, through {w.mtd_end} "
        f"({w.elapsed} of {w.days_in_month} days, {w.days_left} left)"
    )
    table_rows = daily_rows if params.include_daily else weekly_rows
    columns = _PACING_DAILY_MARKDOWN if params.include_daily else _PACING_WEEKLY_MARKDOWN
    resp = make_table_response(rows=table_rows, columns=columns, title=title, extra=extra, fmt=fmt)
    if fmt == "markdown":
        resp.rendered = _render_pacing_markdown(
            title, summary, weekly_rows, extra["targets"], params.include_daily, resp.rendered
        )
    return resp


def _render_pacing_markdown(
    title: str,
    s: Mapping[str, Any],
    weekly: list[dict[str, Any]],
    targets: Mapping[str, Any],
    include_daily: bool,
    table_md: str,
) -> str:
    mtd, fc, var = s["mtd"], s.get("forecast_mtd"), s.get("variance_vs_forecast_mtd")
    ly, pp, pm = s["last_year_mtd"], s["prior_period"], s["prior_month_to_date"]
    rr, fm, tg, rd = (
        s["run_rate_projection"],
        s.get("month_forecast"),
        s.get("to_go"),
        s.get("required_daily_average"),
    )
    lines = [f"### {title}", ""]
    fc_bit = (
        f" vs forecast {_money(fc['demand'])} ({_pct(var['demand_pct'])})" if fc and var else ""
    )
    lines.append(
        f"- **Demand MTD** {_money(mtd['demand'])}{fc_bit} · LY {_money(ly['demand'])} "
        f"({_pct(ly['demand_change_pct'])}) · prior {pp['days']}d {_money(pp['demand'])} "
        f"({_pct(pp['demand_change_pct'])}) · last month same days {_money(pm['demand'])} "
        f"({_pct(pm['demand_change_pct'])})"
    )
    if fm and tg and rd:
        pvf = s.get("projection_vs_forecast") or {}
        lines.append(
            f"- **Month** run-rate {_money(rr['demand'])} vs forecast {_money(fm['demand'])} "
            f"({_pct(pvf.get('demand_pct'))}) · to go {_money(tg['demand'])} · need "
            f"{_money(rd['demand'])}/day over {s['days_left']} days"
        )
    else:
        lines.append(f"- **Month** run-rate {_money(rr['demand'])} (no forecast loaded)")
    sp_fc = f" vs {_money(fc['spend'])} ({_pct(var['spend_pct'])})" if fc and var else ""
    nc_fc = (
        f" vs {_num(fc['new_customers'])} ({_pct(var['new_customers_pct'])})" if fc and var else ""
    )
    lines.append(
        f"- **Spend MTD** {_money(mtd['spend'])}{sp_fc} · MER {_num(mtd['mer'], 2)}"
        + (f" (forecast {_num(fc['mer'], 2)})" if fc and fc.get("mer") is not None else "")
    )
    lines.append(
        f"- **New customers MTD** {_num(mtd['new_customers'])}{nc_fc} · CAC {_money(mtd['cac'])}"
        + (f" (forecast {_money(fc['cac'])})" if fc and fc.get("cac") is not None else "")
        + f" · orders {_num(mtd['orders'])} · AOV {_money(mtd['aov'])}"
    )
    if targets.get("status") == "ok":
        src = targets.get("source") or {}
        lines.append(
            f'- targets: tab "{targets.get("month_tab")}" of {src.get("title", "the pacing sheet")}, '
            f"refreshed {str(src.get('refreshed_at', '?'))[:10]}"
        )
    else:
        lines.append(f"- targets: **{targets.get('status')}** — {targets.get('note')}")
    lines += ["", "#### Weeks", "", render_markdown_table(weekly, columns=_PACING_WEEKLY_MARKDOWN)]
    if include_daily:
        lines += ["", "#### Days", "", table_md.split("\n\n", 1)[-1]]
    return "\n".join(lines)
