"""Tests for the DTC Slack brief (scripts/dtc_brief.py).

The brief is an interpretation layer over `tools/dtc.py`: it picks the windows,
rolls tool rows into a few sentences and decides what deserves a "Watch" flag.
Those decisions are pinned here, hermetically, on hand-built payloads. The `bq`
tier then runs all three modes end to end through `gather()` against a fixture
warehouse of literal rows (the same rows `tests/test_pacing.py` uses, so the
numbers can be checked by hand), because the renderers read dozens of keys off
the tool payloads and a renamed key would otherwise surface only in production.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from bpd_mcp.tools import dtc

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


brief = _load("dtc_brief", ROOT / "scripts" / "dtc_brief.py")
# The pacing fixtures: Aug 2026 with TODAY = Thu 2026-08-06, MTD demand 75 on two
# orders (both first orders), spend 150; targets 10/day demand, 20/day spend.
pacing_fx = _load("pacing_fixtures", ROOT / "tests" / "test_pacing.py")

TODAY = pacing_fx.TODAY


# ---------- windows ----------


@pytest.mark.parametrize(
    ("today", "start", "end"),
    [
        (date(2026, 9, 7), date(2026, 8, 31), date(2026, 9, 6)),  # a Monday: the week just ended
        (date(2026, 9, 10), date(2026, 8, 31), date(2026, 9, 6)),  # Thursday: still that week
        (date(2026, 9, 13), date(2026, 8, 31), date(2026, 9, 6)),  # Sunday: today is not closed
        (date(2026, 9, 14), date(2026, 9, 7), date(2026, 9, 13)),  # next Monday rolls forward
    ],
)
def test_last_complete_week_is_the_latest_closed_monday_to_sunday(
    today: date, start: date, end: date
) -> None:
    assert brief.last_complete_week(today) == (start, end)
    assert start.weekday() == 0 and end.weekday() == 6 and end < today


def test_settled_sheet_days_drop_the_two_days_before_the_export() -> None:
    """The team keys the sheet's actuals by hand the next morning, so the export's
    last day or two is routinely partial and must not be reconciled."""
    p = {
        "daily": [{"day": f"2026-09-{d:02d}"} for d in range(1, 11)],
        "targets": {"source": {"refreshed_at": "2026-09-11T14:00:00+00:00"}},
    }
    assert [d["day"] for d in brief.settled_sheet_days(p)] == [
        f"2026-09-{d:02d}" for d in range(1, 10)
    ]
    p["targets"]["source"]["refreshed_at"] = "2026-09-10T14:00:00+00:00"
    assert brief.settled_sheet_days(p)[-1]["day"] == "2026-09-08"
    # No parseable export date: nothing is filtered rather than everything.
    p["targets"] = {}
    assert len(brief.settled_sheet_days(p)) == 10


# ---------- interpretation ----------


def _pacing(**over: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "month": "2026-09",
        "complete_through": "2026-09-10",
        "elapsed_days": 10,
        "days_in_month": 30,
        "days_left": 20,
        "mtd": {
            "demand": 96_000.0,
            "orders": 1800,
            "new_customers": 690,
            "spend": 30_100.0,
            "mer": 3.19,
            "cac": 43.6,
            "nc_demand": 48_200.0,
            "nc_roas": 1.6,
        },
        "forecast_mtd": {
            "demand": 103_900.0,
            "spend": 35_000.0,
            "new_customers": 824.0,
            "mer": 2.97,
            "cac": 42.5,
            "nc_demand": 56_000.0,
            "nc_roas": 1.6,
        },
        "variance_vs_forecast_mtd": {
            "demand_pct": -7.6,
            "spend_pct": -14.0,
            "new_customers_pct": -16.2,
            "nc_demand_pct": -14.0,
            "nc_roas_pct": 0.0,
        },
        "month_forecast": {"demand": 316_100.0, "spend": 105_000.0, "new_customers": 2470.0},
        "to_go": {"demand": 220_100.0},
        "required_daily_average": {"demand": 11_003.0},
        "run_rate_projection": {"demand": 288_100.0},
        "projection_vs_forecast": {"demand_pct": -8.9},
        "prior_period": {"demand_change_pct": 1.0},
        "prior_month_to_date": {"demand_change_pct": 3.2},
        "last_year_mtd": {"demand_change_pct": 121.1},
        "last_year_note": None,
    }
    s.update(over)
    return {
        "summary": s,
        "daily": [],
        "weekly": [],
        "targets": {
            "status": "ok",
            "month_tab": "September 2026",
            "source": {"refreshed_at": "2026-09-11T14:00:00+00:00"},
        },
    }


def test_watch_flags_only_material_misses_and_spend_room() -> None:
    flags = brief.watch_block(_pacing())
    # Demand is -7.6%: inside the 10% band, so not flagged; NCs and NC demand are.
    assert [f.split(" is ")[0] for f in flags] == ["New customers", "NC demand", "Spend"]
    assert "room to scale" in flags[-1]
    over = _pacing(variance_vs_forecast_mtd={"demand_pct": -12.0, "spend_pct": 11.0})
    flags = brief.watch_block(over)
    assert flags[0].startswith("Demand is 12% behind plan")
    assert flags[1] == "Spend is 11% over plan month-to-date."


def test_watch_flags_missing_targets_and_the_ly_note() -> None:
    p = _pacing(last_year_note="Meta history starts 2025-07-02")
    p["targets"] = {"status": "missing_month", "note": "run the refresh"}
    flags = brief.watch_block(p)
    assert flags[-2] == "Meta history starts 2025-07-02."
    assert flags[-1] == "No targets loaded for this month (missing_month). run the refresh"


def test_sheet_gap_flag_ignores_unsettled_rows() -> None:
    p = _pacing()
    p["daily"] = [
        {
            "day": "2026-09-02",
            "demand": 9_491.0,
            "sheet_actual_demand": 10_159.0,
            "sheet_vs_warehouse_pct": 7.0,
        },
        # The day before the export: partial in the sheet, must not be flagged.
        {
            "day": "2026-09-10",
            "demand": 8_290.0,
            "sheet_actual_demand": 3_888.0,
            "sheet_vs_warehouse_pct": -53.1,
        },
    ]
    gap = [f for f in brief.watch_block(p) if f.startswith("The pacing sheet")]
    assert len(gap) == 1 and "on 1 day(s)" in gap[0] and "2026-09-02" in gap[0]


def test_traffic_light_bands() -> None:
    assert brief._light(0.0) == " :large_green_circle:"
    assert brief._light(-9.9) == " :large_yellow_circle:"
    assert brief._light(-10.1) == " :red_circle:"
    assert brief._light(None) == ""
    # For a cost-type metric a positive variance is the bad direction.
    assert brief._light(5.0, good_when_high=False) == " :large_yellow_circle:"


def test_footer_shows_both_dates_when_pacing_runs_past_the_window() -> None:
    p = _pacing()
    f = brief.footer(p, date(2026, 9, 6))
    assert "data through Sun Sep 6 (pacing through Thu Sep 10)" in f
    assert '"September 2026" tab' in f and "refreshed 2026-09-11" in f
    assert "(pacing through" not in brief.footer(p, date(2026, 9, 10))


def test_sum_rows_recomputes_ratios_from_sums() -> None:
    rows = [
        {"spend": 100.0, "orders": 4, "new_customers": 2, "demand": 300.0, "gross_sales": 320.0},
        {"spend": 50.0, "orders": 1, "new_customers": 0, "demand": 0.0, "gross_sales": 0.0},
    ]
    t = brief._sum_rows(rows)
    assert (t["spend"], t["orders"], t["demand"]) == (150.0, 5.0, 300.0)
    # MER and AOV are on demand (the sheet's definition), never on list-price gross.
    assert t["mer"] == pytest.approx(2.0) and t["cac"] == pytest.approx(75.0)
    assert brief._sum_rows([])["mer"] is None  # no spend: no ratio, never a zero


def test_pulse_with_one_closed_day_says_so() -> None:
    d = {
        "mode": "pulse",
        "today": date(2026, 9, 8),
        "wtd": (date(2026, 9, 7), date(2026, 9, 7)),
        "pacing": _pacing(),
        "cur": {
            "rows": [{"spend": 3_000.0, "orders": 150, "new_customers": 60, "demand": 8_000.0}]
        },
        "prev": {"rows": []},
    }
    out = brief.render_pulse(d)
    main = out["main"]
    assert main.startswith("🛒 **DTC — week so far** (Mon Sep 7)")
    assert "**1 day in:**" in main and "Only one day of the week has closed" in main
    assert "n/a vs the same days last week" in main  # no prior rows: no made-up change
    assert "**September pacing** (through Thu Sep 10, 10 of 30 days)" in main
    assert out["replies"] == []


def test_weekly_refuses_to_render_without_its_week() -> None:
    d = {
        "mode": "weekly",
        "today": date(2026, 9, 7),
        "week": (date(2026, 8, 31), date(2026, 9, 6)),
        "pacing": _pacing(),
        "weeks": {"rows": [{"period": "2026-08-24", "demand": 1.0}]},
    }
    with pytest.raises(RuntimeError, match="no efficiency row for the week of 2026-08-31"):
        brief.render_weekly(d)


def test_cli_emits_main_and_replies_as_json(monkeypatch: Any, capsys: Any) -> None:
    canned = {"main": "hello", "replies": ["thread"]}
    seen: dict[str, Any] = {}

    def fake_build(mode: str, as_of: date | None, targets_path: str | None) -> dict[str, Any]:
        seen.update(mode=mode, as_of=as_of, targets_path=targets_path)
        return canned

    monkeypatch.setattr(brief, "build", fake_build)
    argv = ["--mode", "recap", "--as-of", "2026-09-01", "--targets", "/x/t.json", "--json"]
    assert brief.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == canned
    assert seen == {"mode": "recap", "as_of": date(2026, 9, 1), "targets_path": "/x/t.json"}
    assert brief.main(["--mode", "pulse"]) == 0
    out = capsys.readouterr().out
    assert (
        out.startswith("hello\n") and "[threaded reply]" in out and out.rstrip().endswith("thread")
    )


# ---------- tier 2: all three modes end to end on literal rows ----------

REVENUE_LINES = [
    {
        "revenue_date": r["order_date_ct"],
        "order_id": r["order_id"],
        "channel_bucket": r["channel_bucket"],
        "purchase_type": "One Time",
        "gross_revenue": r["gross_line"],
        "allocated_refund": 0.0,
        "admin_net_revenue": r["net_line"],
    }
    for r in pacing_fx.ORDER_LINES
]


def _tables() -> dict[str, Any]:
    return {**pacing_fx._tables(), "dtc_revenue_lines": REVENUE_LINES}


@pytest.mark.bq
async def test_weekly_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)  # Thu 2026-08-06
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("weekly", warehouse=wh, targets=pacing_fx._targets())
    assert d["week"] == (date(2026, 7, 27), date(2026, 8, 2))
    out = brief.render(d)
    main = out["main"]
    # Week of Jul 27: p1 (30) on Jul 30 + o1 (40 + 5 shipping, counted once) on Aug 1 -> 75,
    # the same order-level demand the pacing block reports for o1 (45), not its gross (40).
    assert main.startswith("🛒 **DTC — week of Mon Jul 27–Sun Aug 2**\nDemand $75, ")
    assert "MER 0.75x" in main  # 75 demand / 100 spend (Aug 1)
    assert "**August pacing** (through Wed Aug 5, 5 of 31 days)" in main
    assert "Demand $75 vs plan $50 (+50.0%) :large_green_circle:" in main
    assert "NC demand $75 vs $40 plan (+87.5%)" in main
    assert "data through Sun Aug 2 (pacing through Wed Aug 5)" in main
    assert len(out["replies"]) == 1 and out["replies"][0].startswith("**")
    assert "Jul 27" in out["replies"][0]


@pytest.mark.bq
async def test_pulse_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("pulse", warehouse=wh, targets=pacing_fx._targets())
    assert d["wtd"] == (date(2026, 8, 3), date(2026, 8, 5))
    main = brief.render(d)["main"]
    # Mon Aug 3..Wed Aug 5: o2 only (gross 25, spend 50 on Aug 3); Jul 27..29 had nothing.
    assert main.startswith(
        "🛒 **DTC — week so far** (Mon Aug 3–Wed Aug 5)\n**3 days in:** demand $30 "
    )
    assert "(n/a vs the same days last week)" in main
    assert "spend $50 (n/a)" in main
    assert "**August pacing** (through Wed Aug 5, 5 of 31 days)" in main


@pytest.mark.bq
async def test_recap_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 9, 1))
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("recap", warehouse=wh, targets=pacing_fx._targets())
    assert set(d) == {"mode", "today", "pacing"}  # one tool call; the week table is pacing's
    out = brief.render(d)
    main = out["main"]
    # August closed at demand 75 against the sheet's 310 total: a miss, no "Beat." lead.
    assert main.startswith(
        "🛒 **DTC — August 2026 recap**\nDemand $75 vs the $310 forecast (-75.8%) :red_circle:"
    )
    assert "**Beat.**" not in main
    # Spend and NCs are judged against the sheet's month Total, the same base as demand.
    assert "Spend $150 vs $620 forecast (-75.8%)" in main
    assert "New customers 2 vs 31 goal (-93.5%) :red_circle:" in main
    assert "Best day Sat Aug 1 at $45; softest" in main
    assert "data through Mon Aug 31" in main and "(pacing through" not in main
    assert "**August 2026 by week**" in out["replies"][0]


@pytest.mark.bq
async def test_recap_without_targets_for_the_month_still_renders(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    """July has no tab in the fixture targets: the recap falls back to period-over-period."""
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 8, 1))
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("recap", warehouse=wh, targets=pacing_fx._targets())
    main = brief.render(d)["main"]
    assert main.startswith("🛒 **DTC — July 2026 recap**\nDemand $90 ")
    assert "(no forecast loaded)" in main
    assert "No targets loaded for this month" in main
