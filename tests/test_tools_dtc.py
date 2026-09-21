"""Tool-surface tests for tools/dtc.py — Phase 2 of the DTC performance / pacing work.

TIER. Every NUMBER below is computed by the real BigQuery engine over literal
fixture CTEs (`@pytest.mark.bq`, 0 bytes billed; see conftest.py for why there
is no local engine double). The checks that provably return before a query is
planned — window validation, a missing logical table, the pure-python period
helpers, and the bucket enum's pin to the registry constant — run in the
default tier against a client that fails the test if touched.

FIXTURES. The column names are the ones the Phase 1 registry bodies PROJECT
(`order_date_ct`, `channel_bucket`, `is_paid_order`, `gross_line`,
`admin_net_revenue`, `spend`, `delivery_status`, ...), and conftest's `_TYPES`
pins them to the types those bodies promise, so role resolution lands on the
production names and the arithmetic runs on the production types. Every
expected figure is worked out by hand in the fixture's own comment.

WINDOW. Fixtures live in two Monday-anchored weeks, 2026-08-03..08-16, and
every test passes that window explicitly so `today` never lands inside it —
which is what keeps `partial_period` False and the suite from rotting.
"""

from __future__ import annotations

import typing
from datetime import date
from typing import Any

import pytest

from bpd_mcp import schemas
from bpd_mcp.bq import DTC_SOURCE_BUCKETS, BigQueryWarehouse
from bpd_mcp.schemas import (
    AdsPerformanceInput,
    DtcSalesSummaryInput,
    MarketingEfficiencyInput,
    SubscriptionHealthInput,
)
from bpd_mcp.tools import dtc
from bpd_mcp.tools.dtc import (
    get_ads_performance,
    get_dtc_sales_summary,
    get_marketing_efficiency,
    get_subscription_health,
)

# ---------------------------------------------------------------------------
# Offline scaffolding
# ---------------------------------------------------------------------------


class _NeverQueried:
    """Stands in for a `bigquery.Client` the code under test must not touch."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - only on failure
        raise AssertionError(f"BigQuery client was touched (.{name}) in an offline test")


def _offline(registry: dict[str, Any] | None = None) -> BigQueryWarehouse:
    return BigQueryWarehouse(client=_NeverQueried(), registry=registry or {})


WINDOW = {"start_date": date(2026, 8, 3), "end_date": date(2026, 8, 16)}
WK1, WK2 = date(2026, 8, 3), date(2026, 8, 10)

# ---------------------------------------------------------------------------
# Fixture rows. Every expected number below is hand-computable from these.
# ---------------------------------------------------------------------------

# Line grain. o1 has a product line and a Checkout+ line (non-product, $0).
# o3 is a ShopMy gifting order: unpaid, list value 90. o5 is an unrecognised
# source with no customer id.
ORDER_LINES = [
    {
        "order_date_ct": "2026-08-03",
        "order_id": "o1",
        "customer_id": 1,
        "order_source": "web",
        "channel_bucket": "core_d2c",
        "purchase_type": "One Time",
        "is_paid_order": True,
        "is_product_line": True,
        "order_subtotal": 36.0,
        "order_shipping": 4.0,
        "quantity": 2,
        "gross_line": 40.0,
        "net_line": 36.0,
    },
    {
        "order_date_ct": "2026-08-03",
        "order_id": "o1",
        "customer_id": 1,
        "order_source": "web",
        "channel_bucket": "core_d2c",
        "purchase_type": "One Time",
        "is_paid_order": True,
        "is_product_line": False,
        "order_subtotal": 36.0,
        "order_shipping": 4.0,
        "quantity": 1,
        "gross_line": 0.0,
        "net_line": 0.0,
    },
    {
        "order_date_ct": "2026-08-04",
        "order_id": "o2",
        "customer_id": 2,
        "order_source": "subscription_contract_checkout_one",
        "channel_bucket": "core_d2c",
        "purchase_type": "Subscription",
        "is_paid_order": True,
        "is_product_line": True,
        "order_subtotal": 25.0,
        "order_shipping": 0.0,
        "quantity": 1,
        "gross_line": 25.0,
        "net_line": 25.0,
    },
    {
        "order_date_ct": "2026-08-05",
        "order_id": "o3",
        "customer_id": 3,
        "order_source": "242196283393",
        "channel_bucket": "gifting",
        "purchase_type": "One Time",
        "is_paid_order": False,
        "is_product_line": True,
        "order_subtotal": 0.0,
        "order_shipping": 0.0,
        "quantity": 3,
        "gross_line": 90.0,
        "net_line": 0.0,
    },
    {
        "order_date_ct": "2026-08-11",
        "order_id": "o4",
        "customer_id": 1,
        "order_source": "web",
        "channel_bucket": "core_d2c",
        "purchase_type": "One Time",
        "is_paid_order": True,
        "is_product_line": True,
        "order_subtotal": 18.0,
        "order_shipping": 2.0,
        "quantity": 1,
        "gross_line": 20.0,
        "net_line": 18.0,
    },
    {
        "order_date_ct": "2026-08-12",
        "order_id": "o5",
        "customer_id": None,
        "order_source": "zzz",
        "channel_bucket": "unknown",
        "purchase_type": "One Time",
        "is_paid_order": True,
        "is_product_line": True,
        "order_subtotal": 10.0,
        "order_shipping": 0.0,
        "quantity": 1,
        "gross_line": 10.0,
        "net_line": 10.0,
    },
]
# core_d2c wk 08-03: orders 2 (o1, o2), customers 2, units 3 (Checkout+ excluded),
#                    gross 65, net_line 61, aov 32.5, demand 65 (o1 36+4 once, o2 25)
# core_d2c wk 08-10: orders 1 (o4), customers 1, units 1, gross 20, net 18, aov 20, demand 20
# gifting  wk 08-03: orders 0, unpaid_orders 1, unpaid_list_value 90
# unknown  wk 08-10: orders 1, gross 10

# Certified view, order-date basis. admin_net = gross - allocated_refund here
# (no discounts in the fixture) except gifting, which nets to 0.
REVENUE_LINES = [
    {
        "revenue_date": "2026-08-03",
        "order_id": "o1",
        "channel_bucket": "core_d2c",
        "purchase_type": "One Time",
        "gross_revenue": 40.0,
        "allocated_refund": 6.0,
        "admin_net_revenue": 34.0,
    },
    {
        "revenue_date": "2026-08-04",
        "order_id": "o2",
        "channel_bucket": "core_d2c",
        "purchase_type": "Subscription",
        "gross_revenue": 25.0,
        "allocated_refund": 0.0,
        "admin_net_revenue": 25.0,
    },
    {
        "revenue_date": "2026-08-05",
        "order_id": "o3",
        "channel_bucket": "gifting",
        "purchase_type": "One Time",
        "gross_revenue": 90.0,
        "allocated_refund": 0.0,
        "admin_net_revenue": 0.0,
    },
    {
        "revenue_date": "2026-08-11",
        "order_id": "o4",
        "channel_bucket": "core_d2c",
        "purchase_type": "One Time",
        "gross_revenue": 20.0,
        "allocated_refund": 2.0,
        "admin_net_revenue": 18.0,
    },
    {
        "revenue_date": "2026-08-12",
        "order_id": "o5",
        "channel_bucket": "unknown",
        "purchase_type": "One Time",
        "gross_revenue": 10.0,
        "allocated_refund": 0.0,
        "admin_net_revenue": 10.0,
    },
]
# core_d2c wk 08-03: gross_revenue 65, refunds_allocated 6, admin_net 59
# core_d2c wk 08-10: gross_revenue 20, refunds_allocated 2, admin_net 18

FIRST_ORDERS = [
    {"first_order_date": "2026-08-03", "customer_id": 1, "lifetime_orders": 2},
    {"first_order_date": "2026-08-04", "customer_id": 2, "lifetime_orders": 1},
]
# wk 08-03: new_customers 2 ; wk 08-10: 0

SPEND = [
    {
        "date": "2026-08-03",
        "channel": "meta",
        "campaign_id": "c1",
        "spend": 100.0,
        "impressions": 10000,
        "clicks": 200,
        "conversions": 4.0,
        "conversion_value": 400.0,
    },
    {
        "date": "2026-08-04",
        "channel": "meta",
        "campaign_id": "c1",
        "spend": 100.0,
        "impressions": 10000,
        "clicks": 200,
        "conversions": 6.0,
        "conversion_value": 600.0,
    },
    {
        "date": "2026-08-03",
        "channel": "google",
        "campaign_id": "g1",
        "spend": 50.0,
        "impressions": 1000,
        "clicks": 50,
        "conversions": 2.0,
        "conversion_value": 300.0,
    },
    {
        "date": "2026-08-11",
        "channel": "google",
        "campaign_id": "g1",
        "spend": 50.0,
        "impressions": 2000,
        "clicks": 40,
        "conversions": 0.0,
        "conversion_value": 0.0,
    },
]
# meta   wk 08-03: spend 200, impr 20000, clicks 400, ctr .02, cpc .5, cpm 10,
#                  conv 10, cpa 20, value 1000, roas 5, days_with_spend_rows 2
# google wk 08-03: spend 50, impr 1000, clicks 50, ctr .05, cpc 1, cpm 50,
#                  conv 2, cpa 25, value 300, roas 6
# google wk 08-10: spend 50, impr 2000, clicks 40, conv 0 -> cpa NULL, roas 0
# all    wk 08-03: spend 250, value 1300 -> platform_roas 5.2
# all    wk 08-10: spend 50 (google only)


def _status_rows() -> list[dict[str, Any]]:
    rows = []
    # meta, wk 08-03: 6 DELIVERED + 1 ABSENT_UNDIAGNOSED (08-05). No wk 08-10 rows.
    for d in range(3, 10):
        status = "ABSENT_UNDIAGNOSED" if d == 5 else "DELIVERED"
        rows.append(
            {
                "event_date": f"2026-08-{d:02d}",
                "channel": "meta",
                "delivery_status": status,
                "spend_modelled": 0.0,
            }
        )
    # google, wk 08-03: 1 DELIVERED + 6 CONFIRMED_NO_DELIVERY; wk 08-10: 7 DELIVERED.
    for d in range(3, 17):
        status = "DELIVERED" if d == 3 or d >= 10 else "CONFIRMED_NO_DELIVERY"
        rows.append(
            {
                "event_date": f"2026-08-{d:02d}",
                "channel": "google",
                "delivery_status": status,
                "spend_modelled": 0.0,
            }
        )
    return rows


DELIVERY = _status_rows()
# meta   wk 08-03: delivered 6, confirmed_zero 0, undiagnosed 1, classified 7 -> undiagnosed_gap
# meta   wk 08-10: no rows at all                                              -> unclassified 7
# google wk 08-03: delivered 1, confirmed_zero 6, classified 7                 -> ok
# google wk 08-10: delivered 7                                                 -> ok

CAMPAIGNS = [
    {
        "campaign_start_date": "2025-07-02",
        "campaign_id": "c1",
        "channel": "meta",
        "campaign_name": "Prospecting",
        "status": "ACTIVE",
    },
    {
        "campaign_start_date": "2021-11-18",
        "campaign_id": "g1",
        "channel": "google",
        "campaign_name": "Brand Search",
        "status": "ENABLED",
    },
]


def _dtc_tables() -> dict[str, Any]:
    return {
        "dtc_order_lines": ORDER_LINES,
        "dtc_revenue_lines": REVENUE_LINES,
        "dtc_customer_first_order": FIRST_ORDERS,
    }


def _ads_tables() -> dict[str, Any]:
    return {
        "ads_spend_daily": SPEND,
        "media_delivery_status": DELIVERY,
        "ads_campaigns": CAMPAIGNS,
    }


def _by(rows: list[dict[str, Any]], *keys: str) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(r[k] for k in keys): r for r in rows}


# ---------------------------------------------------------------------------
# Default tier: pins and the paths that return before any query is planned
# ---------------------------------------------------------------------------


def test_bucket_enum_is_the_registry_constant_plus_unknown() -> None:
    """`schemas.DtcChannelBucket` is spelled out for MCP clients; it must not
    drift from `bq.DTC_SOURCE_BUCKETS`, which is what `channel_bucket` is
    rendered from."""
    assert set(typing.get_args(schemas.DtcChannelBucket)) == set(DTC_SOURCE_BUCKETS) | {"unknown"}
    assert set(dtc.ALL_BUCKETS) == set(typing.get_args(schemas.DtcChannelBucket))


def test_default_bucket_is_core_d2c_only() -> None:
    assert DtcSalesSummaryInput().buckets == ["core_d2c"]


def test_window_defaults_are_ninety_days_inclusive_ending_today(monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 9, 10))
    start, end = dtc.resolve_window(None, None)
    assert (start, end) == (date(2026, 6, 13), date(2026, 9, 10))
    assert (end - start).days + 1 == dtc.DEFAULT_LOOKBACK_DAYS
    # An explicit end anchors the default start; an explicit start is kept.
    assert dtc.resolve_window(None, date(2026, 8, 16)) == (date(2026, 5, 19), date(2026, 8, 16))
    assert dtc.resolve_window(date(2026, 8, 1), None) == (date(2026, 8, 1), date(2026, 9, 10))


@pytest.mark.parametrize(
    ("grain", "period", "lo", "hi"),
    [
        ("day", date(2026, 8, 5), date(2026, 8, 5), date(2026, 8, 5)),
        ("week", date(2026, 8, 3), date(2026, 8, 3), date(2026, 8, 9)),
        ("month", date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 31)),
        ("month", date(2026, 2, 1), date(2026, 2, 1), date(2026, 2, 28)),
        ("month", date(2028, 2, 1), date(2028, 2, 1), date(2028, 2, 29)),
    ],
)
def test_period_bounds(grain: str, period: date, lo: date, hi: date) -> None:
    assert dtc.period_bounds(grain, period) == (lo, hi)


def test_period_expr_weeks_are_monday_anchored() -> None:
    """The one dialect trap worth pinning: DATE_TRUNC(x, WEEK) is SUNDAY-anchored
    and parses fine. Every week here must say WEEK(MONDAY) explicitly."""
    assert dtc.period_expr("week", "d") == "DATE_TRUNC(d, WEEK(MONDAY))"
    assert dtc.period_expr("month", "d") == "DATE_TRUNC(d, MONTH)"
    assert dtc.period_expr("day", "d") == "d"


def test_partial_period_flags_clipping_and_today() -> None:
    today = date(2026, 9, 10)
    # A full week strictly inside the window, in the past: complete.
    assert dtc.is_partial("week", WK1, date(2026, 8, 3), date(2026, 8, 16), today) is False
    # Clipped by the window on the left / right.
    assert dtc.is_partial("week", WK1, date(2026, 8, 4), date(2026, 8, 16), today) is True
    assert dtc.is_partial("week", WK2, date(2026, 8, 3), date(2026, 8, 15), today) is True
    # Reaches today: data is still landing.
    assert (
        dtc.is_partial("week", date(2026, 9, 7), date(2026, 8, 3), date(2026, 9, 13), today) is True
    )
    assert dtc.is_partial("day", today, date(2026, 8, 3), today, today) is True
    assert (
        dtc.is_partial("day", today - (today - date(2026, 9, 9)), date(2026, 8, 3), today, today)
        is False
    )


def test_days_in_window_clips_the_period() -> None:
    assert dtc.days_in_window("week", WK1, date(2026, 8, 3), date(2026, 8, 16)) == 7
    assert dtc.days_in_window("week", WK2, date(2026, 8, 3), date(2026, 8, 12)) == 3
    assert dtc.days_in_window("month", date(2026, 8, 1), date(2026, 8, 20), date(2026, 8, 25)) == 6
    assert dtc.days_in_window("week", WK2, date(2026, 8, 3), date(2026, 8, 9)) == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda wh: get_dtc_sales_summary(
            wh, DtcSalesSummaryInput(start_date=date(2026, 8, 10), end_date=date(2026, 8, 3))
        ),
        lambda wh: get_ads_performance(
            wh, AdsPerformanceInput(start_date=date(2026, 8, 10), end_date=date(2026, 8, 3))
        ),
        lambda wh: get_marketing_efficiency(
            wh, MarketingEfficiencyInput(start_date=date(2026, 8, 10), end_date=date(2026, 8, 3))
        ),
        lambda wh: get_subscription_health(
            wh, SubscriptionHealthInput(start_date=date(2026, 8, 10), end_date=date(2026, 8, 3))
        ),
    ],
)
async def test_inverted_window_is_rejected_before_any_query(call: Any) -> None:
    resp = await call(_offline())
    assert resp.ok is False
    assert resp.error.code == "INVALID_DATE_RANGE"


@pytest.mark.parametrize(
    ("call", "missing"),
    [
        (lambda wh: get_dtc_sales_summary(wh, DtcSalesSummaryInput(**WINDOW)), "dtc_order_lines"),
        (lambda wh: get_ads_performance(wh, AdsPerformanceInput(**WINDOW)), "ads_spend_daily"),
        (
            lambda wh: get_marketing_efficiency(wh, MarketingEfficiencyInput(**WINDOW)),
            "dtc_order_lines",
        ),
        (
            lambda wh: get_subscription_health(wh, SubscriptionHealthInput(**WINDOW)),
            "dtc_subscriptions",
        ),
    ],
)
async def test_missing_logical_table_is_data_unavailable_without_a_query(
    call: Any, missing: str
) -> None:
    resp = await call(_offline())
    assert resp.ok is False
    assert resp.error.code == "DATA_UNAVAILABLE"
    assert resp.error.details["dataset"] == missing


def test_bucket_enum_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        DtcSalesSummaryInput(buckets=["retail"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        DtcSalesSummaryInput(buckets=[])


# ---------------------------------------------------------------------------
# -m bq: the numbers, on the real engine
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_dtc_sales_summary_core_d2c_by_week(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables())
    resp = await get_dtc_sales_summary(
        wh, DtcSalesSummaryInput(grain="week", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period", "bucket")
    assert set(rows) == {(WK1, "core_d2c"), (WK2, "core_d2c")}

    w1 = rows[(WK1, "core_d2c")]
    assert w1["orders"] == 2
    assert w1["customers"] == 2
    assert w1["new_customers"] == 2
    assert w1["units"] == 3  # the Checkout+ line is not a product line
    assert w1["gross_sales"] == pytest.approx(65.0)
    assert w1["net_line_sales"] == pytest.approx(61.0)
    assert w1["aov"] == pytest.approx(32.5)
    assert w1["gross_revenue"] == pytest.approx(65.0)
    assert w1["refunds_allocated"] == pytest.approx(6.0)
    assert w1["admin_net_revenue"] == pytest.approx(59.0)
    assert w1["unpaid_orders"] == 0
    assert w1["partial_period"] is False
    assert "purchase_type" not in w1

    w2 = rows[(WK2, "core_d2c")]
    assert w2["orders"] == 1
    assert w2["new_customers"] == 0  # customer 1 is a repeat buyer
    assert w2["units"] == 1
    assert w2["gross_sales"] == pytest.approx(20.0)
    assert w2["admin_net_revenue"] == pytest.approx(18.0)
    assert w2["aov"] == pytest.approx(20.0)

    # Buckets not selected are surfaced, never dropped.
    other = resp.data["other_buckets"]
    assert set(other) == {"gifting", "unknown"}
    assert other["gifting"]["orders"] == 0
    assert other["gifting"]["unpaid_orders"] == 1
    assert other["gifting"]["unpaid_list_value"] == pytest.approx(90.0)
    assert other["gifting"]["admin_net_revenue"] == pytest.approx(0.0)
    assert other["unknown"]["orders"] == 1
    assert other["unknown"]["gross_sales"] == pytest.approx(10.0)
    assert resp.data["unknown_source_orders"] == 1
    assert resp.data["unknown_source_note"] is not None
    assert resp.data["resolved_columns"]["dtc_order_lines"]["gross"] == "gross_line"
    assert resp.data["resolved_columns"]["dtc_revenue_lines"]["net"] == "admin_net_revenue"
    assert resp.data["window"] == {
        "start": "2026-08-03",
        "end": "2026-08-16",
        "start_defaulted": False,
        "end_defaulted": False,
        "time_zone": "America/Chicago",
    }
    assert "WEEK(MONDAY)" in resp.data["sql"]


@pytest.mark.bq
async def test_dtc_sales_summary_selected_buckets_become_rows(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables())
    resp = await get_dtc_sales_summary(
        wh,
        DtcSalesSummaryInput(
            grain="week", buckets=["core_d2c", "gifting"], response_format="json", **WINDOW
        ),
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period", "bucket")
    g = rows[(WK1, "gifting")]
    assert g["orders"] == 0
    assert g["gross_sales"] == pytest.approx(0.0)  # list value is NOT sales
    assert g["unpaid_orders"] == 1
    assert g["unpaid_list_value"] == pytest.approx(90.0)
    assert g["gross_revenue"] == pytest.approx(90.0)  # the view still carries list value
    assert g["admin_net_revenue"] == pytest.approx(0.0)  # ...and nets it to zero
    assert g["new_customers"] is None  # a gifting recipient never mints a customer
    assert g["aov"] is None  # SAFE_DIVIDE(0, 0)
    assert set(resp.data["other_buckets"]) == {"unknown"}


@pytest.mark.bq
async def test_dtc_sales_summary_by_purchase_type(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables())
    resp = await get_dtc_sales_summary(
        wh,
        DtcSalesSummaryInput(grain="week", by_purchase_type=True, response_format="json", **WINDOW),
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period", "bucket", "purchase_type")
    ot = rows[(WK1, "core_d2c", "One Time")]
    sub = rows[(WK1, "core_d2c", "Subscription")]
    assert (ot["orders"], ot["gross_sales"], ot["admin_net_revenue"]) == (1, 40.0, 34.0)
    assert (sub["orders"], sub["gross_sales"], sub["admin_net_revenue"]) == (1, 25.0, 25.0)
    # A first order has no purchase-type split, so the column is NULL here
    # rather than repeated on every row where it would be summed twice.
    assert ot["new_customers"] is None and sub["new_customers"] is None
    assert rows[(WK2, "core_d2c", "One Time")]["gross_sales"] == pytest.approx(20.0)


@pytest.mark.bq
async def test_dtc_sales_summary_day_grain_and_month_grain(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables())
    day = await get_dtc_sales_summary(
        wh, DtcSalesSummaryInput(grain="day", response_format="json", **WINDOW)
    )
    assert day.ok is True, day.error
    by_day = _by(day.data["rows"], "period")
    assert set(by_day) == {(date(2026, 8, 3),), (date(2026, 8, 4),), (date(2026, 8, 11),)}
    assert by_day[(date(2026, 8, 3),)]["new_customers"] == 1
    assert by_day[(date(2026, 8, 4),)]["new_customers"] == 1

    month = await get_dtc_sales_summary(
        wh, DtcSalesSummaryInput(grain="month", response_format="json", **WINDOW)
    )
    assert month.ok is True, month.error
    (m,) = month.data["rows"]
    assert m["period"] == date(2026, 8, 1)
    assert m["orders"] == 3
    assert m["gross_sales"] == pytest.approx(85.0)
    assert m["admin_net_revenue"] == pytest.approx(77.0)
    assert m["new_customers"] == 2
    # August is clipped by the 08-03..08-16 window, so the month is partial.
    assert m["partial_period"] is True


@pytest.mark.bq
async def test_dtc_sales_summary_markdown_is_narrow(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables())
    resp = await get_dtc_sales_summary(wh, DtcSalesSummaryInput(grain="week", **WINDOW))
    assert resp.ok is True, resp.error
    header = resp.rendered.splitlines()[2]
    assert header.startswith("| period | bucket | orders | new_customers | units | gross_sales |")
    assert "unpaid_list_value" not in header  # in data, not in the rendered table
    assert "DTC sales summary (week, 2026-08-03..2026-08-16, buckets=core_d2c)" in resp.rendered


@pytest.mark.bq
async def test_ads_performance_by_week_with_delivery_integrity(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_ads_tables())
    resp = await get_ads_performance(
        wh, AdsPerformanceInput(grain="week", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period", "channel")
    assert set(rows) == {(WK1, "meta"), (WK1, "google"), (WK2, "google")}

    m1 = rows[(WK1, "meta")]
    assert m1["spend"] == pytest.approx(200.0)
    assert m1["impressions"] == 20000
    assert m1["clicks"] == 400
    assert m1["conversions"] == pytest.approx(10.0)
    assert m1["conversion_value"] == pytest.approx(1000.0)
    assert m1["ctr"] == pytest.approx(0.02)
    assert m1["cpc"] == pytest.approx(0.5)
    assert m1["cpm"] == pytest.approx(10.0)
    assert m1["cpa"] == pytest.approx(20.0)
    assert m1["platform_roas"] == pytest.approx(5.0)
    assert m1["days_with_spend_rows"] == 2
    assert m1["days_delivered"] == 6
    assert m1["days_confirmed_zero"] == 0
    assert m1["days_undiagnosed"] == 1
    assert m1["days_classified"] == 7
    assert m1["days_in_window"] == 7
    assert m1["days_unclassified"] == 0
    assert m1["delivery_flag"] == "undiagnosed_gap"
    assert m1["partial_period"] is False

    g1 = rows[(WK1, "google")]
    assert g1["spend"] == pytest.approx(50.0)
    assert g1["ctr"] == pytest.approx(0.05)
    assert g1["cpc"] == pytest.approx(1.0)
    assert g1["cpa"] == pytest.approx(25.0)
    assert g1["platform_roas"] == pytest.approx(6.0)
    assert g1["days_delivered"] == 1
    assert g1["days_confirmed_zero"] == 6
    assert g1["delivery_flag"] == "ok"

    g2 = rows[(WK2, "google")]
    assert g2["conversions"] == pytest.approx(0.0)
    assert g2["cpa"] is None  # SAFE_DIVIDE by zero, not a failed query
    assert g2["platform_roas"] == pytest.approx(0.0)
    assert g2["days_delivered"] == 7
    assert g2["delivery_flag"] == "ok"

    integrity = resp.data["integrity"]
    assert integrity["undiagnosed_days"] == 1
    # Meta has no status rows in week 2 and no spend rows either, so there is
    # no (period, channel) key to hang a row on; the undiagnosed day is the
    # only flag, and it names its period and channel.
    assert integrity["flagged_periods"] == [
        {
            "period": "2026-08-03",
            "channel": "meta",
            "flag": "undiagnosed_gap",
            "days_undiagnosed": 1,
            "days_unclassified": 0,
        }
    ]
    assert resp.data["resolved_columns"]["ads_spend_daily"]["spend"] == "spend"
    assert resp.data["campaign_names_available"] is True


@pytest.mark.bq
async def test_ads_performance_delivery_only_period_gets_a_row_not_a_drop(
    fixture_warehouse: Any,
) -> None:
    """The view says the channel was live (or confirmed dark) but the fact has no
    rows: that is a row with NULL measures and the delivery counts, not silence."""
    wh = fixture_warehouse(
        ads_spend_daily=[
            r for r in SPEND if not (r["channel"] == "google" and r["date"] == "2026-08-11")
        ],
        media_delivery_status=DELIVERY,
    )
    resp = await get_ads_performance(
        wh, AdsPerformanceInput(grain="week", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    g2 = _by(resp.data["rows"], "period", "channel")[(WK2, "google")]
    assert g2["spend"] is None
    assert g2["platform_roas"] is None
    assert g2["days_with_spend_rows"] == 0
    assert g2["days_delivered"] == 7
    assert g2["delivery_flag"] == "ok"
    assert resp.data["campaign_names_available"] is False


@pytest.mark.bq
async def test_ads_performance_unclassified_days_are_counted(fixture_warehouse: Any) -> None:
    """A window that reaches past the view's last classified day: the missing
    days are `days_unclassified`, flagged, and the period is not called complete."""
    wh = fixture_warehouse(
        ads_spend_daily=SPEND,
        # Google week 2 has spend on 08-11 but only 08-10..08-12 classified.
        media_delivery_status=[
            r for r in DELIVERY if not (r["channel"] == "google" and r["event_date"] > "2026-08-12")
        ],
    )
    resp = await get_ads_performance(
        wh, AdsPerformanceInput(grain="week", channel="google", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period", "channel")
    assert set(rows) == {(WK1, "google"), (WK2, "google")}  # channel filter applied to both queries
    g2 = rows[(WK2, "google")]
    assert g2["days_classified"] == 3
    assert g2["days_unclassified"] == 4
    assert g2["delivery_flag"] == "unclassified_days"
    assert resp.data["integrity"]["unclassified_days"] == 4


@pytest.mark.bq
async def test_ads_performance_by_campaign_joins_names_and_ranks_by_window_spend(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(**_ads_tables())
    resp = await get_ads_performance(
        wh,
        AdsPerformanceInput(
            grain="week", by_campaign=True, top_n=1, response_format="json", **WINDOW
        ),
    )
    assert resp.ok is True, resp.error
    # top_n=1 by WINDOW spend: meta c1 (200) beats google g1 (100), so only c1 rows.
    rows = resp.data["rows"]
    assert [(r["period"], r["channel"], r["campaign_id"]) for r in rows] == [(WK1, "meta", "c1")]
    assert rows[0]["campaign_name"] == "Prospecting"
    assert rows[0]["campaign_status"] == "ACTIVE"
    assert rows[0]["spend"] == pytest.approx(200.0)
    assert "delivery_flag" not in rows[0]
    # Delivery integrity moves to extra in campaign mode, per (period, channel).
    delivery = _by(resp.data["delivery"], "period", "channel")
    assert delivery[(WK1, "meta")]["delivery_flag"] == "undiagnosed_gap"
    assert delivery[(WK2, "google")]["days_delivered"] == 7

    both = await get_ads_performance(
        wh,
        AdsPerformanceInput(
            grain="week", by_campaign=True, top_n=5, response_format="json", **WINDOW
        ),
    )
    assert {(r["channel"], r["campaign_id"]) for r in both.data["rows"]} == {
        ("meta", "c1"),
        ("google", "g1"),
    }


@pytest.mark.bq
async def test_ads_performance_markdown_columns(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_ads_tables())
    resp = await get_ads_performance(wh, AdsPerformanceInput(grain="week", **WINDOW))
    assert resp.ok is True, resp.error
    header = resp.rendered.splitlines()[2]
    assert (
        header
        == "| period | channel | spend | clicks | conversions | conversion_value | platform_roas | delivery_flag | partial_period |"
    )


@pytest.mark.bq
async def test_marketing_efficiency_by_week(fixture_warehouse: Any) -> None:
    wh = fixture_warehouse(**_dtc_tables(), **_ads_tables())
    resp = await get_marketing_efficiency(
        wh, MarketingEfficiencyInput(grain="week", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    rows = _by(resp.data["rows"], "period")
    assert set(rows) == {(WK1,), (WK2,)}

    w1 = rows[(WK1,)]
    assert w1["spend"] == pytest.approx(250.0)
    assert w1["meta_spend"] == pytest.approx(200.0)
    assert w1["google_spend"] == pytest.approx(50.0)
    assert w1["orders"] == 2
    assert w1["customers"] == 2
    assert w1["new_customers"] == 2
    assert w1["gross_sales"] == pytest.approx(65.0)  # gifting's 90 is NOT here
    assert w1["demand"] == pytest.approx(65.0)  # o1's order-level 36+4 counted ONCE over 2 lines
    # o1 (cust 1, first order Aug 3) and o2 (cust 2, first order Aug 4) are both first orders.
    assert (w1["nc_orders"], w1["nc_demand"]) == (2, pytest.approx(65.0))
    assert w1["nc_roas"] == pytest.approx(65.0 / 250.0)
    assert w1["nc_aov"] == pytest.approx(32.5)
    assert w1["net_line_sales"] == pytest.approx(61.0)
    assert w1["admin_net_revenue"] == pytest.approx(59.0)
    assert w1["mer_demand"] == pytest.approx(65.0 / 250.0)
    assert w1["mer_gross"] == pytest.approx(65.0 / 250.0)
    assert w1["mer_net"] == pytest.approx(59.0 / 250.0)
    assert w1["blended_cac"] == pytest.approx(125.0)
    assert w1["cost_per_order"] == pytest.approx(125.0)
    assert w1["new_customer_share"] == pytest.approx(1.0)
    assert w1["platform_conversions"] == pytest.approx(12.0)
    assert w1["platform_conversion_value"] == pytest.approx(1300.0)
    assert w1["platform_roas"] == pytest.approx(5.2)
    assert w1["channels_reporting"] == "both"
    assert w1["partial_period"] is False

    w2 = rows[(WK2,)]
    assert w2["spend"] == pytest.approx(50.0)
    assert w2["orders"] == 1
    assert w2["new_customers"] == 0
    assert w2["blended_cac"] is None  # no new customers: NULL, never inf or a crash
    assert w2["demand"] == pytest.approx(20.0)  # o4: 18 + 2 shipping
    assert (w2["nc_orders"], w2["nc_demand"]) == (0, 0.0)  # o4 is customer 1's REPEAT order
    assert w2["nc_roas"] == pytest.approx(0.0) and w2["nc_aov"] is None
    assert w2["mer_demand"] == pytest.approx(0.4)
    assert w2["mer_gross"] == pytest.approx(0.4)
    assert w2["cost_per_order"] == pytest.approx(50.0)
    assert w2["platform_roas"] == pytest.approx(0.0)
    assert w2["channels_reporting"] == "google_only"
    assert resp.data["periods_before_meta_history"] == []


@pytest.mark.bq
async def test_marketing_efficiency_spend_without_sales_is_a_row_with_zeros(
    fixture_warehouse: Any,
) -> None:
    """The spine is the union of every side's periods: a week of spend with no
    orders must show MER 0 and CAC NULL, not disappear."""
    wh = fixture_warehouse(
        dtc_order_lines=[r for r in ORDER_LINES if r["order_date_ct"] < "2026-08-10"],
        dtc_revenue_lines=[r for r in REVENUE_LINES if r["revenue_date"] < "2026-08-10"],
        dtc_customer_first_order=FIRST_ORDERS,
        ads_spend_daily=SPEND,
    )
    resp = await get_marketing_efficiency(
        wh, MarketingEfficiencyInput(grain="week", response_format="json", **WINDOW)
    )
    assert resp.ok is True, resp.error
    w2 = _by(resp.data["rows"], "period")[(WK2,)]
    assert w2["spend"] == pytest.approx(50.0)
    assert w2["orders"] == 0
    assert w2["gross_sales"] == pytest.approx(0.0)
    assert w2["demand"] == pytest.approx(0.0)
    assert w2["mer_gross"] == pytest.approx(0.0)
    assert w2["mer_demand"] == pytest.approx(0.0)
    assert w2["blended_cac"] is None
    assert w2["cost_per_order"] is None


@pytest.mark.bq
async def test_marketing_efficiency_flags_periods_before_meta_history(
    fixture_warehouse: Any,
) -> None:
    wh = fixture_warehouse(
        **_dtc_tables(),
        ads_spend_daily=[
            *SPEND,
            {
                "date": "2025-06-02",
                "channel": "google",
                "campaign_id": "g1",
                "spend": 10.0,
                "impressions": 100,
                "clicks": 5,
                "conversions": 1.0,
                "conversion_value": 50.0,
            },
        ],
    )
    resp = await get_marketing_efficiency(
        wh,
        MarketingEfficiencyInput(
            grain="week",
            start_date=date(2025, 6, 2),
            end_date=date(2026, 8, 16),
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    early = _by(resp.data["rows"], "period")[(date(2025, 6, 2),)]
    assert early["channels_reporting"] == "google_only"
    assert resp.data["periods_before_meta_history"] == ["2025-06-02"]


@pytest.mark.bq
async def test_schema_incompatible_names_the_role_and_the_columns(fixture_warehouse: Any) -> None:
    """A registry body that stops projecting a role's column must fail loudly
    with the diagnostic, exactly like the Target tools do."""
    bad = [{k: v for k, v in r.items() if k != "gross_line"} for r in ORDER_LINES]
    wh = fixture_warehouse(
        dtc_order_lines=bad, dtc_revenue_lines=REVENUE_LINES, dtc_customer_first_order=FIRST_ORDERS
    )
    resp = await get_dtc_sales_summary(wh, DtcSalesSummaryInput(**WINDOW))
    assert resp.ok is False
    assert resp.error.code == "SCHEMA_INCOMPATIBLE"
    assert resp.error.details["role"] == "gross"
    assert resp.error.details["dataset"] == "dtc_order_lines"
    assert "net_line" in resp.error.details["actual_columns"]


# ---------------------------------------------------------------------------
# bpd_get_subscription_health
# ---------------------------------------------------------------------------
#
# dtc_subscriptions is SCD2 HISTORY (see the registry entry): one row per
# version of a contract, and the tool reads the version whose valid_from is the
# latest before the instant it means. The fixture puts one of every case inside
# the window Aug 3..16:
#
#   k1  customer 1, active throughout            — in both sets, neither count
#   k2  customer 2, starts Aug 4                 — an ADDITION
#   k3  customer 4, starts Aug 6                 — an ADDITION
#   k4  customer 5, cancelled Aug 10 (2 versions)— a REDUCTION
#   k5  customer 1's SECOND contract, Aug 12     — neither: already a subscriber
#
# so subscribers go 2 -> 3 on 2 additions and 1 reduction, while CONTRACTS go
# 2 -> 4. MRR at the close is k1 (30+5, monthly) + k2 (24 / 2mo) + k3 (60 / 3mo)
# + k5 (12, monthly) = 35 + 12 + 20 + 12 = 79.
SUBSCRIPTIONS = [
    {
        "subscription_id": "k1",
        "customer_id": 1,
        "status": "ACTIVE",
        "subscription_created_date": "2026-07-15",
        "cancelled_date": None,
        "recurring_price": 30.0,
        "recurring_delivery": 5.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 1,
        "valid_from": "2026-07-15 11:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "k2",
        "customer_id": 2,
        "status": "ACTIVE",
        "subscription_created_date": "2026-08-04",
        "cancelled_date": None,
        "recurring_price": 24.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 2,
        "valid_from": "2026-08-04 11:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "k3",
        "customer_id": 4,
        "status": "ACTIVE",
        "subscription_created_date": "2026-08-06",
        "cancelled_date": None,
        "recurring_price": 60.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 3,
        "valid_from": "2026-08-06 11:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "k4",
        "customer_id": 5,
        "status": "ACTIVE",
        "subscription_created_date": "2026-07-20",
        "cancelled_date": None,
        "recurring_price": 45.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 1,
        "valid_from": "2026-07-20 11:00:00+00",
        "is_current": False,
    },
    {
        "subscription_id": "k4",
        "customer_id": 5,
        "status": "CANCELLED",
        "subscription_created_date": "2026-07-20",
        "cancelled_date": "2026-08-10",
        "recurring_price": 45.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 1,
        "valid_from": "2026-08-10 11:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "k5",
        "customer_id": 1,
        "status": "ACTIVE",
        "subscription_created_date": "2026-08-12",
        "cancelled_date": None,
        "recurring_price": 12.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 1,
        "valid_from": "2026-08-12 11:00:00+00",
        "is_current": True,
    },
]

# o6 is the storefront checkout that STARTS customer 4's subscription; o2 (in
# ORDER_LINES above) is a Loop renewal. Checkout 55, recurring 25.
SUB_ORDER_LINES = [
    *ORDER_LINES,
    {
        "order_date_ct": "2026-08-06",
        "order_id": "o6",
        "customer_id": 4,
        "order_source": "web",
        "channel_bucket": "core_d2c",
        "purchase_type": "Subscription",
        "is_paid_order": True,
        "is_product_line": True,
        "order_subtotal": 50.0,
        "order_shipping": 5.0,
        "quantity": 1,
        "gross_line": 55.0,
        "net_line": 50.0,
        "order_total": 60.0,
        "order_tax": 5.0,
        "order_discounts": 0.0,
        "variant_id": "v1",
    },
]
SUB_FIRST_ORDERS = [
    *FIRST_ORDERS,
    {"first_order_date": "2026-08-06", "customer_id": 4, "lifetime_orders": 1},
]


def _subscription_warehouse(fixture_warehouse: Any, **over: Any) -> Any:
    return fixture_warehouse(
        **{
            "dtc_subscriptions": SUBSCRIPTIONS,
            "dtc_order_lines": SUB_ORDER_LINES,
            "dtc_customer_first_order": SUB_FIRST_ORDERS,
            **over,
        }
    )


@pytest.mark.bq
async def test_subscription_health_counts_subscribers_not_contracts(
    fixture_warehouse: Any,
) -> None:
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(
        wh, SubscriptionHealthInput(**WINDOW, response_format="json")
    )
    assert resp.ok is True, resp.error
    s = resp.data["subscribers"]
    # Customer 1 holds two contracts at the close and still counts once.
    assert (s["active"], s["active_start"]) == (3, 2)
    assert (s["active_subscriptions"], s["active_subscriptions_start"]) == (4, 2)
    assert (s["additions"], s["reductions"], s["net_growth"]) == (2, 1, 1)
    assert resp.data["point_in_time_available"] is True
    assert resp.data["history_start"] == date(2026, 7, 15)


@pytest.mark.bq
async def test_subscription_health_net_growth_always_ties_to_the_two_counts(
    fixture_warehouse: Any,
) -> None:
    """The guarantee the brief's Retention block rests on: whatever the window,
    net growth is additions minus reductions AND the move in active subscribers.
    Nothing in the brief can show three numbers that do not add up."""
    wh = _subscription_warehouse(fixture_warehouse)
    for start, end in (
        (date(2026, 8, 3), date(2026, 8, 16)),
        (date(2026, 8, 3), date(2026, 8, 9)),
        (date(2026, 8, 10), date(2026, 8, 16)),
        (date(2026, 8, 17), date(2026, 8, 23)),  # a window after everything
    ):
        resp = await get_subscription_health(
            wh,
            SubscriptionHealthInput(start_date=start, end_date=end, response_format="json"),
        )
        assert resp.ok is True, resp.error
        s = resp.data["subscribers"]
        assert s["net_growth"] == s["additions"] - s["reductions"], (start, end)
        assert s["net_growth"] == s["active"] - s["active_start"], (start, end)


@pytest.mark.bq
async def test_subscription_health_mrr_normalises_the_billing_interval(
    fixture_warehouse: Any,
) -> None:
    """A contract that bills $60 every three months is $20 of MRR, never $60."""
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(
        wh, SubscriptionHealthInput(**WINDOW, response_format="json")
    )
    s = resp.data["subscribers"]
    assert s["active_mrr"] == pytest.approx(79.0)  # 35 + 12 + 20 + 12
    assert s["active_mrr_start"] == pytest.approx(80.0)  # k1 35 + k4 45


@pytest.mark.bq
async def test_subscription_health_splits_checkout_from_recurring_revenue(
    fixture_warehouse: Any,
) -> None:
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(
        wh, SubscriptionHealthInput(**WINDOW, response_format="json")
    )
    r = resp.data["revenue"]
    # o6 through the storefront starts a subscription; o2 is Loop renewing one.
    assert r["checkout_revenue"] == pytest.approx(55.0) and r["checkout_orders"] == 1
    assert r["recurring_revenue"] == pytest.approx(25.0) and r["recurring_orders"] == 1
    assert r["subscription_revenue"] == pytest.approx(80.0)
    # Demand over the same paid core-D2C orders: gifting and the unknown bucket
    # are out, so 40 + 25 + 20 + 55.
    assert r["total_demand"] == pytest.approx(140.0)
    assert r["subscription_share"] == pytest.approx(80.0 / 140.0)


@pytest.mark.bq
async def test_subscription_health_take_rate_is_new_customers_who_arrived_on_a_plan(
    fixture_warehouse: Any,
) -> None:
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(
        wh, SubscriptionHealthInput(**WINDOW, response_format="json")
    )
    a = resp.data["acquisition"]
    # Three new customers in the window; 2 and 4 arrived on a subscription.
    assert (a["new_customers"], a["new_subscribers"]) == (3, 2)
    assert a["take_rate"] == pytest.approx(2 / 3)


@pytest.mark.bq
async def test_subscription_health_refuses_a_window_before_the_history_starts(
    fixture_warehouse: Any,
) -> None:
    """Point-in-time counts before the first SCD2 snapshot would describe a
    partial book. Null them and say why, rather than report a number that looks
    like a collapse in subscribers."""
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(
        wh,
        SubscriptionHealthInput(
            start_date=date(2026, 7, 1), end_date=date(2026, 8, 16), response_format="json"
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["point_in_time_available"] is False
    assert all(v is None for v in resp.data["subscribers"].values())
    assert "2026-07-15" in resp.data["note"]
    # The revenue and take-rate halves do not depend on the history and stand.
    assert resp.data["revenue"]["subscription_revenue"] == pytest.approx(80.0)


@pytest.mark.bq
async def test_subscription_health_markdown_leads_with_the_book(fixture_warehouse: Any) -> None:
    wh = _subscription_warehouse(fixture_warehouse)
    resp = await get_subscription_health(wh, SubscriptionHealthInput(**WINDOW))
    lines = resp.rendered.splitlines()
    assert lines[0] == "### Subscriber health (2026-08-03..2026-08-16)"
    assert "**Active subscribers** 3 (from 2) · additions 2 · reductions 1 · net 1" in lines[2]
    assert "**Active MRR** $79 over 4 contracts" in lines[3]
    assert "checkout $55 (1 orders) + recurring $25 (1 orders)" in lines[4]
    assert "**Take rate** 66.7% — 2 of 3 new customers" in lines[5]

