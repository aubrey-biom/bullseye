"""Tests for the DTC Slack brief (scripts/dtc_brief.py).

The brief is an interpretation layer over `tools/dtc.py`: it picks the windows,
sums the plan for them, rolls tool rows into scorecard lines and colours each one.
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

from bpd_mcp import pacing_targets as pt
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
# orders (both first orders), spend 150; plan 10/day demand, 20/day spend, 1 NC/day.
pacing_fx = _load("pacing_fixtures", ROOT / "tests" / "test_pacing.py")

TODAY = pacing_fx.TODAY
G, Y, R, W = brief.GREEN, brief.YELLOW, brief.RED, brief.NEUTRAL


# ---------- windows and plan ----------


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


def test_plan_for_sums_the_days_and_derives_the_ratios() -> None:
    t = pacing_fx._targets()  # August only: 10 / 20 / 1 per day
    p = brief.plan_for(t, date(2026, 8, 3), date(2026, 8, 9))
    assert (p["demand"], p["spend"], p["new_customers"]) == (70.0, 140.0, 7.0)
    assert p["mer"] == pytest.approx(0.5) and p["cac"] == pytest.approx(20.0)


def test_plan_for_is_none_when_any_day_lacks_a_forecast() -> None:
    """A week straddling a month whose tab has not landed has NO plan, not half of one."""
    t = pacing_fx._targets()
    p = brief.plan_for(t, date(2026, 7, 27), date(2026, 8, 2))
    assert p == {"demand": None, "spend": None, "new_customers": None, "mer": None, "cac": None}
    assert brief.plan_for(None, date(2026, 8, 1), date(2026, 8, 7))["demand"] is None


# ---------- interpretation ----------


def _pacing(**over: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "month": "2026-09",
        "complete_through": "2026-09-13",
        "elapsed_days": 13,
        "days_in_month": 30,
        "days_left": 17,
        "mtd": {
            "demand": 123_100.0,
            "orders": 2300,
            "new_customers": 892,
            "spend": 40_000.0,
            "mer": 3.08,
            "cac": 44.8,
            "aov": 53.5,
            "nc_demand": 62_700.0,
            "nc_roas": 1.57,
            "platform_roas": 1.3,
        },
        "forecast_mtd": {
            "demand": 134_200.0,
            "spend": 45_500.0,
            "new_customers": 1071.0,
            "mer": 2.95,
            "cac": 42.5,
            "nc_demand": 72_800.0,
            "nc_roas": 1.6,
        },
        "month_forecast": {"demand": 316_100.0, "spend": 105_000.0, "new_customers": 2470.0},
        "to_go": {"demand": 193_000.0},
        "required_daily_average": {"demand": 11_354.0},
        "run_rate_projection": {"demand": 284_000.0},
        "projection_vs_forecast": {"demand_pct": -10.1},
        "prior_period": {"demand_change_pct": 1.0},
        "prior_month_to_date": {"demand_change_pct": 0.6},
        "last_year_mtd": {"demand_change_pct": 129.0},
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


def test_traffic_light_bands() -> None:
    assert brief._light(0.0) == G
    assert brief._light(-9.9) == Y
    assert brief._light(-10.1) == R
    assert brief._light(None) == W  # no plan: nothing to judge against
    # For a cost-type metric a positive variance is the bad direction.
    assert brief._light(5.0, good_when_high=False) == Y
    assert brief._light(-5.0, good_when_high=False) == G


def test_scorecard_colours_each_line_against_its_own_plan() -> None:
    cur = {"demand": 63_400.0, "mer": 3.06, "new_customers": 465, "cac": 44.6, "spend": 20_700.0}
    plan = {"demand": 71_000.0, "mer": 2.9, "new_customers": 576, "cac": 42.5, "spend": 24_500.0}
    prev = {"demand": 68_900.0, "mer": 3.05, "new_customers": 506, "cac": 44.7, "spend": 22_600.0}
    lines = brief.scorecard(cur, plan, compare=prev, compare_label="WoW", prev_label="prior week")
    assert lines[0] == f"{R} **Demand**  $63.4K vs $71.0K plan (-10.7%) · -8.0% WoW"
    assert lines[1] == f"{G} **MER**  3.06x vs 2.90x plan · prior week 3.05x"
    assert lines[2].startswith(f"{R} **New customers**  465 vs 576 plan (-19.3%)")
    assert lines[3] == f"{Y} **CAC**  $45 vs $42 plan · prior week $45"
    assert lines[4] == f"{W} **Spend**  $20.7K vs $24.5K plan (-15.5%) · -8.4% WoW"  # never judged
    # No plan: the actual alone, neutral light, no invented percentage.
    bare = brief.scorecard(cur, dict.fromkeys(plan))
    assert bare[0] == f"{W} **Demand**  $63.4K" and "plan" not in bare[2]


def test_month_block_scores_the_month_and_flags_spend_room() -> None:
    lines = brief.month_block(_pacing())
    assert lines[0] == "**September — day 13 of 30**"
    assert lines[1].startswith(f"{Y} **Demand**  $123.1K vs $134.2K plan (-8.3%) · run-rate")
    assert lines[2] == f"{G} **MER**  3.08x vs 2.95x plan"
    assert lines[3].startswith(f"{R} **New customers**  892 vs 1,071 plan (-16.7%)")
    assert (
        lines[4]
        == f"{R} **NC demand**  $62.7K vs $72.8K plan (-13.9%) · NC ROAS 1.57x vs 1.60x target"
    )
    assert lines[5] == f"{Y} **CAC**  $45 vs $42 plan"
    assert lines[6].endswith("· room to scale if efficiency holds")  # spend >10% under
    assert (
        lines[7]
        == "To go $193.0K over 17 days ($11.4K/day) · vs LY +129.0% · vs same days last month +0.6%"
    )


def test_month_block_for_a_finished_month_has_no_run_rate_or_to_go() -> None:
    p = _pacing(complete_through="2026-08-31", elapsed_days=31, days_in_month=31, days_left=0)
    lines = brief.month_block(p)
    assert lines[0] == "**August — final** (31 days)"
    assert "run-rate" not in lines[1] and not lines[-1].startswith("To go")


def test_notes_only_carry_what_the_lights_cannot_show() -> None:
    assert brief.notes_block(_pacing()) == []
    p = _pacing(last_year_note="Meta history starts 2025-07-02")
    p["targets"] = {"status": "missing_month", "note": "run the refresh"}
    notes = brief.notes_block(p)
    assert notes[0] == "Meta history starts 2025-07-02."
    assert notes[1].startswith("No plan loaded for this month (missing_month)")


def test_footer_shows_both_dates_when_the_month_runs_past_the_window() -> None:
    p = _pacing()
    f = brief.footer(p, date(2026, 9, 6))
    assert "data through Sun Sep 6 (month through Sun Sep 13)" in f
    assert '"September 2026" tab' in f and "refreshed 2026-09-11" in f
    assert "demand = net sales + shipping" in f
    assert "(month through" not in brief.footer(p, date(2026, 9, 13))


def test_sum_rows_recomputes_ratios_from_sums() -> None:
    rows = [
        {"spend": 100.0, "orders": 4, "new_customers": 2, "demand": 300.0, "gross_sales": 320.0},
        {"spend": 50.0, "orders": 1, "new_customers": 0, "demand": 0.0, "gross_sales": 0.0},
    ]
    t = brief._sum_rows(rows)
    assert (t["spend"], t["orders"], t["demand"]) == (150.0, 5.0, 300.0)
    # MER and AOV are on demand (the sheet's definition), never on list-price gross.
    assert t["mer"] == pytest.approx(2.0) and t["cac"] == pytest.approx(75.0)
    assert t["nc_share"] == pytest.approx(0.4)
    assert brief._sum_rows([])["mer"] is None  # no spend: no ratio, never a zero


def test_daily_table_has_a_row_per_day_and_a_total() -> None:
    t = pacing_fx._targets()
    days = {
        date(2026, 8, 3): {"demand": 30.0, "spend": 50.0, "new_customers": 1, "blended_cac": 50.0},
        date(2026, 8, 4): {"demand": 0.0, "spend": 0.0, "new_customers": 0, "blended_cac": None},
    }
    out = brief.daily_table(date(2026, 8, 3), date(2026, 8, 4), days, None, t, title="Days")
    body = out.split("```")[1].strip().splitlines()
    assert body[0].split() == ["Day", "Demand", "vs", "plan", "Spend", "NCs", "CAC"]  # no LY column
    assert body[1].split() == ["Mon", "3", "$30", "+200.0%", "$50", "1", "$50"]
    assert body[2].split() == ["Tue", "4", "$0", "-100.0%", "$0", "0", "n/a"]
    assert body[3].split() == ["Total", "$30", "+50.0%", "$50", "1", "$50"]  # 30 vs 20 plan
    with_ly = brief.daily_table(
        date(2026, 8, 3),
        date(2026, 8, 4),
        days,
        {date(2025, 8, 4): {"demand": 15.0, "spend": 0.0, "new_customers": 0}},
        t,
        title="Days",
    )
    row = with_ly.split("```")[1].strip().splitlines()[1].split()
    assert row[4] == "+100.0%"  # Mon Aug 3 2026 vs Mon Aug 4 2025: 30 vs 15


def test_pulse_with_one_closed_day_says_so() -> None:
    d = {
        "mode": "pulse",
        "today": date(2026, 9, 8),
        "wtd": (date(2026, 9, 7), date(2026, 9, 7)),
        "pacing": _pacing(),
        "days": {
            date(2026, 9, 7): {
                "demand": 8_000.0,
                "spend": 3_000.0,
                "orders": 150,
                "new_customers": 60,
                "blended_cac": 50.0,
            }
        },
        "prev_days": {},
        "plan": {
            "demand": 10_000.0,
            "spend": 3_500.0,
            "new_customers": 80.0,
            "mer": 2.86,
            "cac": 43.75,
        },
        "targets": None,
    }
    out = brief.render_pulse(d)
    main = out["main"]
    assert main.startswith("🛒 **DTC — week so far** (Mon Sep 7)\n\n**1 day in, vs plan**")
    assert f"{R} **Demand**  $8,000 vs $10.0K plan (-20.0%) · n/a WoW" in main  # no prior rows
    assert "Only one day of the week has closed" in main
    assert "**September — day 13 of 30**" in main
    assert len(out["replies"]) == 1 and out["replies"][0].startswith("**Day by day — Mon Sep 7**")


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
    # the same order-level demand the month block reports for o1 (45), not its gross (40).
    # July has no tab in the fixture targets, so the week has NO plan: neutral, no percentage.
    assert main.startswith(
        f"🛒 **DTC — week of Mon Jul 27–Sun Aug 2**\n\n**Week vs plan**\n{W} **Demand**  $75"
    )
    assert f"{W} **MER**  0.75x" in main  # 75 demand / 100 spend (Aug 1)
    assert "**August — day 5 of 31**" in main
    assert f"{G} **Demand**  $75 vs $50 plan (+50.0%)" in main
    assert f"{G} **NC demand**  $75 vs $40 plan (+87.5%) · NC ROAS 0.50x vs 0.40x target" in main
    assert "data through Sun Aug 2 (month through Wed Aug 5)" in main
    assert len(out["replies"]) == 2
    days, trend = out["replies"]
    assert days.startswith("**Day by day — Mon Jul 27–Sun Aug 2**")
    assert "Thu 30" in days and "Total" in days
    assert trend.startswith("**") and "Jul 27" in trend


@pytest.mark.bq
async def test_pulse_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("pulse", warehouse=wh, targets=pacing_fx._targets())
    assert d["wtd"] == (date(2026, 8, 3), date(2026, 8, 5))
    out = brief.render(d)
    main = out["main"]
    # Mon Aug 3..Wed Aug 5: o2 only (25 + 5 shipping, spend 50 on Aug 3) vs a 3-day plan of 30;
    # Jul 27..29 had nothing, so WoW has no base.
    assert "**3 days in, vs plan**" in main
    assert f"{G} **Demand**  $30 vs $30 plan (+0.0%) · n/a WoW" in main
    assert f"{G} **MER**  0.60x vs 0.50x plan" in main
    assert f"{R} **New customers**  1 vs 3 plan (-66.7%) · n/a WoW" in main
    assert "**August — day 5 of 31**" in main
    assert out["replies"][0].startswith("**Day by day — Mon Aug 3–Wed Aug 5**")


@pytest.mark.bq
async def test_recap_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 9, 1))
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("recap", warehouse=wh, targets=pacing_fx._targets())
    assert set(d) == {"mode", "today", "pacing", "month"}
    out = brief.render(d)
    main = out["main"]
    # August closed at demand 75 against the sheet's 310 total: a miss, every line on that base.
    assert main.startswith(
        f"🛒 **DTC — August 2026 recap** — **miss**\n\n**Month vs forecast**\n{R} **Demand**  $75 vs $310 plan (-75.8%)"
    )
    assert f"{R} **New customers**  2 vs 31 plan (-93.5%)" in main
    assert f"{W} **Spend**  $150 vs $620 plan (-75.8%)" in main
    # Certified net revenue for the month: o1 (30 + 10) + o2 (25) = 65 vs demand 75.
    assert "**Net revenue (certified, after refunds)** $65 · -13.3% vs demand" in main
    assert "Best day Sat Aug 1 at $45 · softest" in main
    assert "data through Mon Aug 31" in main and "(month through" not in main
    assert [r.split("\n")[0] for r in out["replies"]] == [
        "**August 2026 by week** (Monday-anchored; first and last are partial)",
        "**August 2026 by day**",
    ]


@pytest.mark.bq
async def test_recap_without_targets_for_the_month_still_renders(
    fixture_warehouse: Any, monkeypatch: Any
) -> None:
    """July has no tab in the fixture targets: neutral lights, actuals only, and a note."""
    monkeypatch.setattr(dtc, "today_reporting", lambda: date(2026, 8, 1))
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("recap", warehouse=wh, targets=pacing_fx._targets())
    main = brief.render(d)["main"]
    assert main.startswith(
        f"🛒 **DTC — July 2026 recap**\n\n**Month vs forecast**\n{W} **Demand**  $90\n"
    )
    assert "No plan loaded for this month" in main


def test_targets_file_carries_forecasts_only() -> None:
    """The sheet is the source of targets and nothing else: no actual column is read."""
    assert all(f.startswith("forecast_") for f in pt.FIELD_MAP)
    t = pt.load_targets(ROOT / "config" / "dtc_pacing_targets.json")
    for m in t.months.values():
        for day in m.days.values():
            assert all(k.startswith("forecast_") for k in day.values)
