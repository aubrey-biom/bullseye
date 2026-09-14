"""The DTC pacing tool and its targets config.

THREE THINGS ARE PINNED HERE.

  1. The checked-in `config/dtc_pacing_targets.json` is well-formed: every
     month has every calendar day, the paced series are present, and the
     sheet's stated Total agrees with the sum of its days. A refresh that
     silently dropped a column or a tab fails here, not in a leadership
     meeting.
  2. `scripts/refresh_pacing_targets.py` reads a workbook shaped like the
     ecomm team's sheet (header row 3, a day per row, a summary block) and is
     tolerant of the things that actually vary: a renamed column becomes a
     missing series, a "V2" tab is skipped, "-" cells become null.
  3. The tool's arithmetic, on the real engine over fixture rows: demand is
     ONE row per order (a two-line order counts once), forecast variance,
     run-rate, to-go, required daily average, the three comparison windows,
     the weekly rows, and the graceful path when a month has no targets.

TIER. Window maths, the loader and the refresh script are pure python and run
in the default tier. The numbers run on BigQuery over literal fixture CTEs
(`@pytest.mark.bq`, 0 bytes billed). `today` is pinned via
`dtc.today_reporting` so the suite cannot rot.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bpd_mcp import pacing_targets as pt
from bpd_mcp.bq import BigQueryWarehouse
from bpd_mcp.schemas import DtcPacingInput
from bpd_mcp.tools import dtc
from bpd_mcp.tools.dtc import get_dtc_pacing

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "dtc_pacing_targets.json"

# ---------------------------------------------------------------------------
# 1. The checked-in targets file
# ---------------------------------------------------------------------------


def test_checked_in_targets_load_and_cover_every_day() -> None:
    t = pt.load_targets(CONFIG)
    assert t.available_months, "no months in the targets file"
    for ym, m in t.months.items():
        first, last = dtc.month_bounds(ym)
        expected_days = (last - first).days + 1
        assert len(m.days) == expected_days, f"{ym}: {len(m.days)} days, expected {expected_days}"
        assert m.first_day == first and m.last_day == last
        for fld in pt.PACED_FIELDS:
            assert any(d.get(fld) is not None for d in m.days.values()), f"{ym}: no {fld}"
        stated = m.totals.get("forecast_demand")
        assert stated is not None, f"{ym}: the sheet's Total row was not captured"
        assert stated == pytest.approx(m.series_sum("forecast_demand"), rel=0.005), (
            f"{ym}: Total row {stated} != sum of days {m.series_sum('forecast_demand')}"
        )
    assert t.source["title"].startswith("2026 Daily Pacing")
    assert t.source["refresh_command"] == pt.REFRESH_COMMAND


def test_checked_in_targets_field_map_matches_the_module() -> None:
    payload = json.loads(CONFIG.read_text())
    assert payload["schema_version"] == pt.SCHEMA_VERSION
    assert payload["source"]["field_map"] == pt.FIELD_MAP


def test_september_2026_targets_are_the_sheets(monkeypatch: Any) -> None:
    """Spot-check against the tab as read on 2026-09-11."""
    m = pt.load_targets(CONFIG).month("2026-09")
    assert m is not None and m.tab == "September 2026"
    assert m.days[date(2026, 9, 1)].get("forecast_demand") == pytest.approx(11246.02)
    assert m.days[date(2026, 9, 1)].get("forecast_spend") == pytest.approx(3500.0)
    assert m.totals["forecast_demand"] == pytest.approx(316096.71)
    assert m.totals["forecast_spend"] == pytest.approx(105000.0)
    assert m.month_total("forecast_new_customers") == pytest.approx(2470.588, rel=1e-4)


# ---------------------------------------------------------------------------
# 1b. The loader's own contract
# ---------------------------------------------------------------------------


def _payload(
    days: dict[str, dict[str, Any]], ym: str = "2026-08", **overrides: Any
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "schema_version": pt.SCHEMA_VERSION,
        "source": {"title": "t"},
        "months": {ym: {"tab": "August 2026", "totals": {}, "days": days}},
    }
    p.update(overrides)
    return p


def test_loader_rejects_wrong_schema_version() -> None:
    with pytest.raises(pt.TargetsUnavailable, match="schema_version"):
        pt.parse_targets(_payload({"2026-08-01": {}}, schema_version=99))


def test_loader_rejects_a_day_outside_its_month() -> None:
    with pytest.raises(pt.TargetsUnavailable, match="outside the month"):
        pt.parse_targets(_payload({"2026-09-01": {"forecast_demand": 1}}))


def test_loader_rejects_a_non_object_totals() -> None:
    """Every shape error must be TargetsUnavailable: the tool's 'targets are never
    fatal' guard catches exactly that and nothing else."""
    bad = _payload({"2026-08-01": {"forecast_demand": 1}})
    bad["months"]["2026-08"]["totals"] = []
    with pytest.raises(pt.TargetsUnavailable, match="totals"):
        pt.parse_targets(bad)


def test_series_sum_is_none_for_an_absent_series() -> None:
    """An absent forecast column is 'no target', never a $0 target."""
    m = pt.parse_targets(
        _payload({"2026-08-01": {"forecast_demand": 10}, "2026-08-02": {"forecast_demand": 12}})
    ).month("2026-08")
    assert m is not None
    assert m.series_sum("forecast_spend") is None
    assert m.series_sum("forecast_demand", date(2026, 8, 5), date(2026, 8, 9)) is None
    assert m.series_sum("forecast_demand") == 22


def test_coerce_number_parses_sheet_strings() -> None:
    assert pt.coerce_number("$1,234.50") == 1234.5
    assert pt.coerce_number("12%") == 12.0
    assert pt.coerce_number("-") is None
    assert pt.coerce_number("#REF!") is None
    assert pt.coerce_number(True) is None
    assert pt.coerce_number(7) == 7.0


def test_load_targets_caches_by_mtime(tmp_path: Path) -> None:
    import os
    import time

    f = tmp_path / "t.json"
    f.write_text(json.dumps(_payload({"2026-08-01": {"forecast_demand": 1}})))
    a = pt.load_targets(f)
    assert pt.load_targets(f) is a  # same mtime -> same object
    f.write_text(json.dumps(_payload({"2026-08-01": {"forecast_demand": 2}})))
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))  # force a distinct mtime
    b = pt.load_targets(f)
    assert b is not a
    aug = b.month("2026-08")
    assert aug is not None and aug.days[date(2026, 8, 1)].get("forecast_demand") == 2


def test_loader_missing_file_carries_the_remediation(tmp_path: Path) -> None:
    with pytest.raises(pt.TargetsUnavailable) as e:
        pt.load_targets(tmp_path / "nope.json")
    assert "refresh_pacing_targets.py" in str(e.value)


def test_month_total_prefers_the_sheets_total_row_then_sums() -> None:
    m = pt.parse_targets(
        _payload(
            {
                "2026-08-01": {"forecast_demand": 10, "forecast_spend": 5},
                "2026-08-02": {"forecast_demand": 12, "forecast_spend": 5},
            },
        )
    ).month("2026-08")
    assert m is not None
    assert m.month_total("forecast_demand") == 22
    assert m.month_total("forecast_new_customers") is None  # never present
    m2 = pt.parse_targets(
        {
            "schema_version": 1,
            "source": {},
            "months": {
                "2026-08": {
                    "tab": "x",
                    "totals": {"forecast_demand": 999},
                    "days": {"2026-08-01": {"forecast_demand": 10}},
                }
            },
        }
    ).month("2026-08")
    assert m2 is not None and m2.month_total("forecast_demand") == 999
    assert m2.series_sum("forecast_demand", date(2026, 8, 1), date(2026, 8, 1)) == 10


# ---------------------------------------------------------------------------
# 2. The refresh script against a workbook shaped like the sheet
# ---------------------------------------------------------------------------

openpyxl = pytest.importorskip("openpyxl")


def _sheet_like_workbook(path: Path) -> None:
    """Two monthly tabs, a V2 revision and a source tab, in the sheet's layout."""
    import datetime as dt

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def month_tab(title: str, first: dt.date, n_days: int, *, rename_spend: bool = False) -> None:
        ws = wb.create_sheet(title)
        ws.cell(1, 1, f"{title.split()[0].upper()} DAILY PACING")
        ws.cell(2, 5, "BLENDED DATA")
        header = [
            "Week",
            None,
            "Date",
            None,
            "Actual DMD",
            "Forecast",
            "LY Total DMD",
            "Forecasted  Spend" if not rename_spend else "Fcst Spend",
            "Actual Spend",
            "Forecasted NCs",
            "Total NCs",
            "Forecasted BRoAS",
        ]
        for c, h in enumerate(header, start=1):
            if h is not None:
                ws.cell(3, c, h)
        for i in range(n_days):
            d = first + dt.timedelta(days=i)
            r = 4 + i
            ws.cell(r, 1, f"wk {i // 7 + 1}")
            ws.cell(r, 3, dt.datetime(d.year, d.month, d.day))
            ws.cell(r, 4, d.strftime("%a"))
            ws.cell(r, 5, 100.0 + i if i < 2 else "-")  # actuals only for two days
            ws.cell(r, 6, 10.0)  # forecast demand
            ws.cell(r, 7, 5.0)
            ws.cell(r, 8, 20.0)  # forecast spend
            ws.cell(r, 9, 19.5 if i < 2 else "-")
            ws.cell(r, 10, 1.0)
            ws.cell(r, 11, 1 if i < 2 else 0)
            ws.cell(r, 12, 0.5)
        r = 4 + n_days
        ws.cell(r, 3, "Total")
        ws.cell(r, 6, 10.0 * n_days)
        ws.cell(r, 8, 20.0 * n_days)
        ws.cell(r, 10, 1.0 * n_days)
        ws.cell(r + 1, 3, "To Date")
        ws.cell(r + 2, 3, "To Go")

    month_tab("July 2026", dt.date(2026, 7, 1), 31)
    month_tab("August 2026", dt.date(2026, 8, 1), 31, rename_spend=True)
    month_tab("August 2026 V2", dt.date(2026, 8, 1), 31)
    src = wb.create_sheet("DAILY SALES")
    src.cell(1, 1, "Date")
    wb.save(path)


def test_refresh_script_reads_the_sheet_layout(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "refresh", ROOT / "scripts" / "refresh_pacing_targets.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    xlsx = tmp_path / "pacing.xlsx"
    _sheet_like_workbook(xlsx)
    out = tmp_path / "targets.json"
    assert mod.main([str(xlsx), "--out", str(out), "--file-id", "abc"]) == 0

    t = pt.load_targets(out)
    assert t.available_months == ["2026-07", "2026-08"]
    assert set(t.source["skipped_tabs"]) == {"August 2026 V2", "DAILY SALES"}
    jul = t.month("2026-07")
    assert jul is not None and len(jul.days) == 31
    d1 = jul.days[date(2026, 7, 1)]
    assert d1.get("forecast_demand") == 10.0
    assert d1.get("forecast_spend") == 20.0  # header had a double space: normalised
    # The sheet's own actuals are not read: targets only.
    assert "sheet_actual_demand" not in d1.values and "Actual DMD" not in t.source["field_map"].values()
    assert jul.totals["forecast_demand"] == 310.0
    assert jul.totals["forecast_spend"] == 620.0
    # The renamed spend column is a MISSING series, never the wrong one.
    aug = t.month("2026-08")
    assert aug is not None
    assert aug.days[date(2026, 8, 1)].get("forecast_spend") is None
    assert aug.totals["forecast_spend"] is None
    assert any("August 2026" in n and "forecast_spend" in n for n in t.source["notes"])

    # --check: current file is current; a changed export is not.
    assert mod.main([str(xlsx), "--out", str(out), "--file-id", "abc", "--check"]) == 0
    _sheet_like_workbook(xlsx)  # same content -> still current
    assert mod.main([str(xlsx), "--out", str(out), "--file-id", "abc", "--check"]) == 0
    assert mod.main([str(xlsx), "--out", str(out), "--file-id", "other", "--check"]) == 1


# ---------------------------------------------------------------------------
# 3a. Window maths (pure python)
# ---------------------------------------------------------------------------

TODAY = date(2026, 8, 6)


def test_windows_pace_through_yesterday_by_default() -> None:
    w = dtc.resolve_pacing_windows(None, None, TODAY)
    assert (w.month, w.month_start, w.month_end, w.days_in_month) == (
        "2026-08",
        date(2026, 8, 1),
        date(2026, 8, 31),
        31,
    )
    assert w.mtd_end == date(2026, 8, 5)
    assert (w.elapsed, w.days_left) == (5, 26)
    assert (w.prior_start, w.prior_end) == (date(2026, 7, 27), date(2026, 7, 31))
    assert (w.prior_month_start, w.prior_month_end) == (date(2026, 7, 1), date(2026, 7, 5))
    assert (w.ly_mtd_start, w.ly_mtd_end) == (
        date(2025, 8, 2),
        date(2025, 8, 6),
    )  # 364 days: weekday-aligned
    assert (w.ly_month_start, w.ly_month_end) == (date(2025, 8, 2), date(2025, 9, 1))
    assert w.query_start == date(2025, 8, 2) and w.query_end == date(2026, 8, 5)


def test_windows_with_a_future_as_of_still_pace_through_yesterday() -> None:
    """A future as_of must not pace phantom zero-actual days."""
    w = dtc.resolve_pacing_windows(date(2026, 12, 15), None, TODAY)
    assert (w.month, w.mtd_end, w.elapsed) == ("2026-08", date(2026, 8, 5), 5)
    with pytest.raises(ValueError, match="no complete day"):
        dtc.resolve_pacing_windows(date(2026, 12, 15), "2026-12", TODAY)


def test_windows_with_a_past_as_of_include_that_day() -> None:
    w = dtc.resolve_pacing_windows(date(2026, 8, 3), None, TODAY)
    assert w.mtd_end == date(2026, 8, 3) and w.elapsed == 3


def test_windows_for_a_finished_month_span_the_whole_month() -> None:
    w = dtc.resolve_pacing_windows(None, "2026-07", TODAY)
    assert (w.month_start, w.mtd_end, w.elapsed, w.days_left) == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        31,
        0,
    )
    assert (w.prior_start, w.prior_end) == (date(2026, 5, 31), date(2026, 6, 30))
    assert (w.prior_month_start, w.prior_month_end) == (
        date(2026, 6, 1),
        date(2026, 6, 30),
    )  # clipped to June's 30


def test_windows_reject_a_month_with_no_complete_day() -> None:
    with pytest.raises(ValueError, match="no complete day"):
        dtc.resolve_pacing_windows(None, "2026-09", TODAY)
    with pytest.raises(ValueError):
        # as_of == today, so the 1st is not complete and August has no paced day yet
        dtc.resolve_pacing_windows(date(2026, 8, 1), "2026-08", date(2026, 8, 1))
    # ...whereas with no month pinned the same call falls back to July, which is complete.
    assert dtc.resolve_pacing_windows(date(2026, 8, 1), None, date(2026, 8, 1)).month == "2026-07"


def test_prior_month_span_clips_to_the_short_month() -> None:
    w = dtc.resolve_pacing_windows(None, "2026-03", date(2026, 4, 1))
    assert (w.prior_month_start, w.prior_month_end) == (date(2026, 2, 1), date(2026, 2, 28))


def test_pct_change_guards() -> None:
    assert dtc._pct_change(150.0, 100.0) == 50.0
    assert dtc._pct_change(50.0, 0.0) is None
    assert dtc._pct_change(None, 10.0) is None
    assert dtc._pct_change(10.0, None) is None


# ---------------------------------------------------------------------------
# 3b. Offline error paths
# ---------------------------------------------------------------------------


class _NeverQueried:
    def __getattr__(self, name: str) -> Any:  # pragma: no cover - only on failure
        raise AssertionError(f"BigQuery client was touched (.{name}) in an offline test")


async def test_missing_table_is_data_unavailable_without_a_query(monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    resp = await get_dtc_pacing(
        BigQueryWarehouse(client=_NeverQueried(), registry={}), DtcPacingInput()
    )
    assert resp.ok is False and resp.error.code == "DATA_UNAVAILABLE"


async def test_future_month_is_rejected_before_any_query(monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    resp = await get_dtc_pacing(
        BigQueryWarehouse(client=_NeverQueried(), registry={}), DtcPacingInput(month="2026-12")
    )
    assert resp.ok is False and resp.error.code == "INVALID_DATE_RANGE"


def test_pacing_input_month_pattern() -> None:
    with pytest.raises(ValueError):
        DtcPacingInput(month="2026-13")
    with pytest.raises(ValueError):
        DtcPacingInput(month="Aug 2026")
    assert DtcPacingInput(month="2026-08").month == "2026-08"


# ---------------------------------------------------------------------------
# 3c. The numbers, on the real engine
# ---------------------------------------------------------------------------

# today = 2026-08-06 -> MTD is Aug 1..5 (5 days), 26 left.
# Order lines (paid core_d2c unless noted). o1 has TWO lines: its order-level
# subtotal/shipping must count ONCE.
ORDER_LINES = [
    {
        "order_date_ct": "2026-08-01",
        "order_id": "o1",
        "customer_id": 1,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 40.0,
        "order_shipping": 5.0,
        "gross_line": 30.0,
        "net_line": 30.0,
    },
    {
        "order_date_ct": "2026-08-01",
        "order_id": "o1",
        "customer_id": 1,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 40.0,
        "order_shipping": 5.0,
        "gross_line": 10.0,
        "net_line": 10.0,
    },
    {
        "order_date_ct": "2026-08-03",
        "order_id": "o2",
        "customer_id": 2,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 25.0,
        "order_shipping": 5.0,
        "gross_line": 25.0,
        "net_line": 25.0,
    },
    # gifting: excluded from demand
    {
        "order_date_ct": "2026-08-03",
        "order_id": "o3",
        "customer_id": 3,
        "channel_bucket": "gifting",
        "is_paid_order": False,
        "order_subtotal": 0.0,
        "order_shipping": 0.0,
        "gross_line": 90.0,
        "net_line": 0.0,
    },
    # prior period (Jul 27..31)
    {
        "order_date_ct": "2026-07-30",
        "order_id": "p1",
        "customer_id": 4,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 30.0,
        "order_shipping": 0.0,
        "gross_line": 30.0,
        "net_line": 30.0,
    },
    # prior month same days (Jul 1..5)
    {
        "order_date_ct": "2026-07-02",
        "order_id": "m1",
        "customer_id": 5,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 60.0,
        "order_shipping": 0.0,
        "gross_line": 60.0,
        "net_line": 60.0,
    },
    # last year, weekday-aligned: 2025-08-02 maps to 2026-08-01
    {
        "order_date_ct": "2025-08-02",
        "order_id": "y1",
        "customer_id": 6,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 20.0,
        "order_shipping": 2.0,
        "gross_line": 20.0,
        "net_line": 20.0,
    },
    # last year, later in the LY month window but outside LY MTD (maps to 2026-08-20)
    {
        "order_date_ct": "2025-08-21",
        "order_id": "y2",
        "customer_id": 7,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 8.0,
        "order_shipping": 0.0,
        "gross_line": 8.0,
        "net_line": 8.0,
    },
]
# MTD demand = (40+5) + (25+5) = 75 ; orders 2 ; net_sales 65 ; shipping 10 ; aov 37.5
# prior period (Jul 27..31) demand 30 ; prior month same days (Jul 1..5) demand 60
# LY MTD (2025-08-02..06) demand 22 ; LY month (2025-08-02..09-01) demand 30
FIRST_ORDERS = [
    {"first_order_date": "2026-08-01", "customer_id": 1, "lifetime_orders": 1},
    {"first_order_date": "2026-08-03", "customer_id": 2, "lifetime_orders": 1},
    {"first_order_date": "2025-08-02", "customer_id": 6, "lifetime_orders": 1},
]
SPEND = [
    {
        "date": "2026-08-01",
        "channel": "meta",
        "campaign_id": "c1",
        "spend": 100.0,
        "impressions": 1,
        "clicks": 1,
        "conversions": 1.0,
        "conversion_value": 300.0,
    },
    {
        "date": "2026-08-03",
        "channel": "google",
        "campaign_id": "g1",
        "spend": 50.0,
        "impressions": 1,
        "clicks": 1,
        "conversions": 1.0,
        "conversion_value": 100.0,
    },
    {
        "date": "2025-08-02",
        "channel": "google",
        "campaign_id": "g1",
        "spend": 10.0,
        "impressions": 1,
        "clicks": 1,
        "conversions": 0.0,
        "conversion_value": 0.0,
    },
]
# MTD spend 150 ; mer 0.5 ; cac 75 ; platform_roas 400/150


def _targets(days: int = 31) -> pt.PacingTargets:
    """Aug 2026: forecast demand 10/day, spend 20/day, 1 new customer/day, NC demand 8/day,
    NC RoAS 0.4 and BRoAS 3.0 (rate targets, constant per day)."""
    d = {
        f"2026-08-{i:02d}": {
            "forecast_demand": 10.0,
            "forecast_spend": 20.0,
            "forecast_new_customers": 1.0,
            "forecast_nc_demand": 8.0,
            "forecast_nc_roas": 0.4,
            "forecast_broas": 3.0,
        }
        for i in range(1, days + 1)
    }
    return pt.parse_targets(
        {
            "schema_version": 1,
            "source": {"title": "fixture sheet", "refreshed_at": "2026-08-06T00:00:00+00:00"},
            "months": {
                "2026-08": {
                    "tab": "August 2026",
                    "totals": {
                        "forecast_demand": 310.0,
                        "forecast_spend": 620.0,
                        "forecast_new_customers": 31.0,
                    },
                    "days": d,
                }
            },
        }
    )


def _tables() -> dict[str, Any]:
    return {
        "dtc_order_lines": ORDER_LINES,
        "dtc_customer_first_order": FIRST_ORDERS,
        "ads_spend_daily": SPEND,
    }


@pytest.mark.bq
async def test_pacing_month_to_date_against_targets(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    resp = await get_dtc_pacing(wh, DtcPacingInput(response_format="json"), targets=_targets())
    assert resp.ok is True, resp.error
    s = resp.data["summary"]
    assert (s["month"], s["complete_through"], s["elapsed_days"], s["days_left"]) == (
        "2026-08",
        "2026-08-05",
        5,
        26,
    )

    mtd = s["mtd"]
    assert mtd["demand"] == pytest.approx(75.0)  # o1 counted ONCE despite two lines
    assert mtd["net_sales"] == pytest.approx(65.0)
    assert mtd["shipping"] == pytest.approx(10.0)
    assert mtd["orders"] == 2
    assert mtd["new_customers"] == 2
    assert mtd["spend"] == pytest.approx(150.0)
    assert mtd["aov"] == pytest.approx(37.5)
    assert mtd["mer"] == pytest.approx(0.5)
    assert mtd["cac"] == pytest.approx(75.0)
    assert mtd["platform_roas"] == pytest.approx(400.0 / 150.0)
    # Both MTD orders are the customers' first paid core-D2C orders -> all demand is NC demand.
    assert mtd["nc_orders"] == 2
    assert mtd["nc_demand"] == pytest.approx(75.0)
    assert mtd["nc_roas"] == pytest.approx(0.5)  # nc_demand / spend
    assert mtd["nc_aov"] == pytest.approx(37.5)

    assert s["forecast_mtd"]["demand"] == pytest.approx(50.0)
    assert s["forecast_mtd"]["nc_demand"] == pytest.approx(40.0)
    assert s["forecast_mtd"]["nc_roas"] == pytest.approx(0.4)  # mean of a constant series
    assert s["forecast_mtd"]["broas"] == pytest.approx(3.0)
    assert s["variance_vs_forecast_mtd"]["nc_demand_pct"] == pytest.approx(87.5)
    assert s["variance_vs_forecast_mtd"]["nc_roas_pct"] == pytest.approx(25.0)
    assert s["month_forecast"]["nc_demand"] == pytest.approx(248.0)
    assert s["forecast_mtd"]["spend"] == pytest.approx(100.0)
    assert s["forecast_mtd"]["new_customers"] == pytest.approx(5.0)
    assert s["forecast_mtd"]["cac"] == pytest.approx(20.0)
    assert s["variance_vs_forecast_mtd"]["demand_pct"] == pytest.approx(50.0)
    assert s["variance_vs_forecast_mtd"]["spend_pct"] == pytest.approx(50.0)
    assert s["variance_vs_forecast_mtd"]["new_customers_pct"] == pytest.approx(-60.0)
    assert s["month_forecast"]["demand"] == pytest.approx(310.0)  # the sheet's Total row
    assert s["to_go"]["demand"] == pytest.approx(235.0)
    assert s["required_daily_average"]["demand"] == pytest.approx(235.0 / 26)
    assert s["run_rate_projection"]["demand"] == pytest.approx(75.0 / 5 * 31)
    assert s["projection_vs_forecast"]["demand_pct"] == pytest.approx(
        (465.0 - 310.0) / 310.0 * 100, abs=0.01
    )

    assert s["prior_period"]["window"] == {"start": "2026-07-27", "end": "2026-07-31"}
    assert s["prior_period"]["demand"] == pytest.approx(30.0)
    assert s["prior_period"]["demand_change_pct"] == pytest.approx(150.0)
    assert s["prior_month_to_date"]["window"] == {"start": "2026-07-01", "end": "2026-07-05"}
    assert s["prior_month_to_date"]["demand"] == pytest.approx(60.0)
    assert s["prior_month_to_date"]["demand_change_pct"] == pytest.approx(25.0)
    assert s["last_year_mtd"]["window"] == {"start": "2025-08-02", "end": "2025-08-06"}
    assert s["last_year_mtd"]["demand"] == pytest.approx(22.0)
    assert s["last_year_mtd"]["spend"] == pytest.approx(10.0)
    assert s["last_year_mtd"]["new_customers"] == 1
    assert s["last_year_mtd"]["nc_demand"] == pytest.approx(22.0)
    assert s["last_year_mtd"]["nc_demand_change_pct"] == pytest.approx(
        (75.0 - 22.0) / 22.0 * 100, abs=0.01
    )
    assert s["last_year_mtd"]["demand_change_pct"] == pytest.approx(
        (75.0 - 22.0) / 22.0 * 100, abs=0.01
    )
    assert s["last_year_month"]["demand"] == pytest.approx(30.0)  # y1 + y2

    # Daily rows: one per MTD day, zeros where nothing happened, LY aligned by weekday.
    days = {str(r["day"]): r for r in resp.data["rows"]}
    assert list(days) == ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
    d1 = days["2026-08-01"]
    assert (d1["dow"], d1["demand"], d1["orders"], d1["forecast_demand"]) == ("Sat", 45.0, 1, 10.0)
    assert d1["act_vs_fcst_pct"] == pytest.approx(350.0)
    assert (str(d1["ly_day"]), d1["ly_demand"]) == ("2025-08-02", 22.0)
    assert (d1["nc_orders"], d1["nc_demand"]) == (1, 45.0)
    assert d1["act_vs_ly_pct"] == pytest.approx((45.0 - 22.0) / 22.0 * 100, abs=0.01)
    assert "sheet_actual_demand" not in d1  # actuals are the warehouse's alone
    d2 = days["2026-08-02"]
    assert (d2["demand"], d2["orders"], d2["spend"], d2["forecast_demand"]) == (0.0, 0, 0.0, 10.0)
    assert d2["act_vs_fcst_pct"] == pytest.approx(-100.0)
    assert d2["act_vs_ly_pct"] is None  # LY day had nothing: no base

    # Weekly rows: Aug 1 2026 is a Saturday -> wk 1 is the Mon Jul 27 week clipped to Aug 1..2.
    weeks = resp.data["weekly"]
    assert [w["week"] for w in weeks] == ["wk 1", "wk 2"]
    assert (
        str(weeks[0]["week_start"]),
        weeks[0]["days"],
        weeks[0]["demand"],
        weeks[0]["forecast_demand"],
    ) == ("2026-07-27", 2, 45.0, 20.0)
    assert weeks[0]["partial"] is True
    assert (str(weeks[1]["week_start"]), weeks[1]["days"], weeks[1]["demand"]) == (
        "2026-08-03",
        3,
        30.0,
    )
    assert weeks[1]["partial"] is True  # runs past mtd_end

    assert resp.data["targets"]["status"] == "ok"
    assert resp.data["targets"]["month_tab"] == "August 2026"
    assert resp.data["resolved_columns"]["dtc_order_lines"]["order_shipping"] == "order_shipping"


@pytest.mark.bq
async def test_pacing_without_targets_still_paces_period_over_period(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    empty = pt.parse_targets(
        {
            "schema_version": 1,
            "source": {},
            "months": {
                "2026-07": {"tab": "July 2026", "days": {"2026-07-01": {"forecast_demand": 1}}}
            },
        }
    )
    resp = await get_dtc_pacing(
        wh, DtcPacingInput(include_daily=False, response_format="json"), targets=empty
    )
    assert resp.ok is True, resp.error
    s = resp.data["summary"]
    assert s["mtd"]["demand"] == pytest.approx(75.0)
    assert (
        s["forecast_mtd"] is None and s["to_go"] is None and s["variance_vs_forecast_mtd"] is None
    )
    assert s["run_rate_projection"]["demand"] == pytest.approx(
        465.0
    )  # actuals-only projection still there
    assert s["prior_period"]["demand_change_pct"] == pytest.approx(150.0)
    assert resp.data["targets"]["status"] == "missing_month"
    assert (
        "2026-07" in resp.data["targets"]["note"]
        and "refresh_pacing_targets.py" in resp.data["targets"]["note"]
    )
    # include_daily=False: the table is the weekly view and no daily block is returned.
    assert [r["week"] for r in resp.data["rows"]] == ["wk 1", "wk 2"]
    assert "daily" not in resp.data


@pytest.mark.bq
async def test_pacing_markdown_leads_with_the_summary(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    resp = await get_dtc_pacing(wh, DtcPacingInput(), targets=_targets())
    assert resp.ok is True, resp.error
    md = resp.rendered
    assert md.startswith("### DTC pacing — 2026-08, through 2026-08-05 (5 of 31 days, 26 left)")
    assert "**Demand MTD** $75 vs forecast $50 (+50.0%)" in md
    assert "run-rate $465 vs forecast $310 (+50.0%)" in md
    assert "to go $235" in md
    assert "**Spend MTD** $150 vs $100 (+50.0%)" in md
    assert 'targets: tab "August 2026" of fixture sheet, refreshed 2026-08-06' in md
    assert "#### Weeks" in md and "#### Days" in md
    assert (
        "| day | dow | demand | forecast_demand | act_vs_fcst_pct | ly_demand | act_vs_ly_pct | spend | new_customers |"
        in md
    )


@pytest.mark.bq
async def test_pacing_finished_month_has_no_days_left(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 9, 15))
    wh = fixture_warehouse(**_tables())
    resp = await get_dtc_pacing(
        wh, DtcPacingInput(month="2026-08", response_format="json"), targets=_targets()
    )
    assert resp.ok is True, resp.error
    s = resp.data["summary"]
    assert (s["elapsed_days"], s["days_left"], s["complete_through"]) == (31, 0, "2026-08-31")
    assert s["required_daily_average"]["demand"] is None  # nothing left to average over
    assert s["forecast_mtd"]["demand"] == pytest.approx(310.0)
    assert s["run_rate_projection"]["demand"] == pytest.approx(75.0)  # the month is what it was
    assert len(resp.data["rows"]) == 31

    md = (await get_dtc_pacing(wh, DtcPacingInput(month="2026-08"), targets=_targets())).rendered
    assert "**Month complete** final $75 vs forecast $310 (-75.8%)" in md
    assert "over 0 days" not in md


@pytest.mark.bq
async def test_pacing_with_a_month_missing_one_forecast_series(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    """The sheet tab lacked 'Forecasted Spend': spend has NO target, not a $0 one."""
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    days = {
        f"2026-08-{i:02d}": {"forecast_demand": 10.0, "forecast_new_customers": 1.0}
        for i in range(1, 32)
    }
    t = pt.parse_targets(
        {
            "schema_version": 1,
            "source": {"title": "s"},
            "months": {
                "2026-08": {
                    "tab": "August 2026",
                    "totals": {"forecast_demand": 310.0},
                    "days": days,
                }
            },
        }
    )
    resp = await get_dtc_pacing(wh, DtcPacingInput(response_format="json"), targets=t)
    assert resp.ok is True, resp.error
    s = resp.data["summary"]
    assert s["forecast_mtd"]["spend"] is None
    assert s["forecast_mtd"]["demand"] == pytest.approx(50.0)
    assert s["variance_vs_forecast_mtd"]["spend_pct"] is None
    assert s["month_forecast"]["spend"] is None and s["to_go"]["spend"] is None
    md = (await get_dtc_pacing(wh, DtcPacingInput(), targets=t)).rendered
    assert "**Spend MTD** $150 · MER 0.50" in md  # no "vs $0"
    assert "vs $0" not in md
