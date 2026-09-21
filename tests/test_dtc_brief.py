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
    assert (p["demand"], p["spend"], p["new_customers"], p["nc_demand"]) == (70.0, 140.0, 7.0, 56.0)
    assert p["mer"] == pytest.approx(0.5) and p["cac"] == pytest.approx(20.0)
    assert p["nc_roas"] == pytest.approx(0.4)  # ratio of the summed series, like MER


def test_plan_for_is_none_when_any_day_lacks_a_forecast() -> None:
    """A week straddling a month whose tab has not landed has NO plan, not half of one."""
    t = pacing_fx._targets()
    p = brief.plan_for(t, date(2026, 7, 27), date(2026, 8, 2))
    assert set(p) == {
        "demand",
        "spend",
        "new_customers",
        "nc_demand",
        "recurring_revenue",
        "mer",
        "nc_roas",
        "cac",
    }
    assert all(v is None for v in p.values())


def test_plan_nc_roas_falls_back_to_the_sheets_rate_column() -> None:
    """A tab with 'Forecasted NC RoAS' but no 'Forecasted NC DMD' still yields a
    week NC ROAS plan (the mean of the daily rates), as the pacing tool does."""
    raw = pacing_fx._targets()
    days = {
        d.isoformat(): {k: v for k, v in t.values.items() if k != "forecast_nc_demand"}
        for d, t in raw.month("2026-08").days.items()
    }
    t = pt.parse_targets(
        {"schema_version": 1, "source": {}, "months": {"2026-08": {"tab": "Aug", "days": days}}}
    )
    p = brief.plan_for(t, date(2026, 8, 3), date(2026, 8, 9))
    assert p["nc_demand"] is None and p["nc_roas"] == pytest.approx(0.4)
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
            "nc_aov": 70.0,
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
    """🟢 needs EVERY comparison favourable, not just the first one."""
    assert brief._light(0.0, 2.0) == G  # on goal and improving
    assert brief._light(5.0, None) == G  # above goal, no prior period to judge
    assert brief._light(5.0, -3.0) == Y  # above goal but slipping vs prior week
    assert brief._light(-14.9, 20.0) == Y
    assert brief._light(-15.0, 20.0) == Y  # the band is inclusive: 15% is watch
    assert brief._light(-15.1, 20.0) == R  # "more than 15%" is where red starts
    assert brief._light(None, None) == W  # no goal, no prior period: nothing to judge
    # Cost-type metrics (CAC, spend, subscriber reductions) read the other way.
    assert brief._light(5.0, good_when_high=False) == Y
    assert brief._light(-5.0, good_when_high=False) == G
    assert brief._light(20.0, good_when_high=False) == R
    # Net growth's own rule: negative is red however small the move.
    assert brief._light(50.0, 50.0, force_red=True) == R


def test_rate_light_judges_at_the_precision_it_prints() -> None:
    """1.5996x and 1.60x both print "1.60x": that line is on goal, not yellow
    over a third decimal the reader cannot see."""
    assert brief._rate_light(1.5996, 1.6, 1.55, brief._x) == G
    assert brief._rate_light(1.30, 1.6, 1.55, brief._x) == R  # -18.8% vs goal
    assert brief._rate_light(44.6, 42.5, 44.7, brief._money, good_when_high=False) == Y


def test_revenue_block_scores_goal_and_prior_week() -> None:
    cur = {"demand": 63_400.0, "nc_roas": 1.5996, "nc_aov": 70.0, "spend": 20_700.0}
    goal = {"demand": 71_000.0, "nc_roas": 1.6, "spend": 24_500.0}
    prev = {"demand": 68_900.0, "nc_roas": 1.55, "spend": 22_600.0}
    lines = brief.revenue_block(cur, goal, prev)
    assert lines[0] == (
        f"{Y} **Total revenue**  $63.4K vs $71.0K goal (-10.7%) · -8.0% vs prior week"
    )
    assert lines[1] == (
        f"{G} **New customer ROAS**  1.60x vs 1.60x goal (+0.0%) · prior week 1.55x · NC AOV $70"
    )
    # Spend is scored against the GOAL only, as a cost: 15% under goal is money
    # not spent, not a miss, and the WoW move never colours the line.
    assert lines[2] == (
        f"{G} **Ad spend**  $20.7K vs $24.5K goal (-15.5%) · -8.4% vs prior week · under goal"
    )
    assert len(lines) == 3
    over = brief.revenue_block({**cur, "spend": 30_600.0}, goal)
    assert over[2] == (
        f"{R} **Ad spend**  $30.6K vs $24.5K goal (+24.9%) · over goal — check efficiency"
    )
    # No goal: the actual alone, neutral light, no invented percentage.
    bare = brief.revenue_block(cur, dict.fromkeys(goal))
    assert bare[0] == f"{W} **Total revenue**  $63.4K"
    assert bare[1] == f"{W} **New customer ROAS**  1.60x · NC AOV $70"


def test_acquisition_block_puts_take_rate_beside_cac_and_the_nc_lines() -> None:
    cur = {"cac": 44.6, "nc_demand": 33_200.0, "new_customers": 465}
    goal = {"cac": 42.5, "nc_demand": 39_200.0, "new_customers": 576}
    prev = {"cac": 44.7, "nc_demand": 35_000.0, "new_customers": 506}
    lines = brief.acquisition_block(
        cur, goal, prev, take_rate=0.564, prev_take_rate=0.541
    )
    assert lines[0] == f"{Y} **New customer CAC**  $45 vs $42 goal (+4.9%) · prior week $45"
    # The sheet has no take-rate goal, so the line is judged on the prior week alone.
    assert lines[1] == f"{G} **Subscriber take rate**  56.4% · prior week 54.1%"
    assert lines[2] == f"{R} **NC demand**  $33.2K vs $39.2K goal (-15.3%) · -5.1% vs prior week"
    assert lines[3] == (
        f"{R} **New customers**  465 vs 576 goal (-19.3%) · -8.1% vs prior week"
    )
    assert len(lines) == 4
    no_subs = brief.acquisition_block(cur, goal, prev)
    assert no_subs[1] == f"{W} **Subscriber take rate**  n/a"


def _subs(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "point_in_time_available": True,
        "note": None,
        "subscribers": {
            "active": 8_807,
            "active_start": 8_613,
            "additions": 311,
            "reductions": 117,
            "net_growth": 194,
            "active_mrr": 158_480.0,
        },
        "revenue": {
            "subscription_revenue": 43_007.0,
            "checkout_revenue": 24_703.0,
            "recurring_revenue": 18_304.0,
        },
        "acquisition": {"take_rate": 0.564},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


PRIOR_SUBS = _subs(
    subscribers={
        "active": 8_613,
        "active_start": 8_430,
        "additions": 290,
        "reductions": 107,
        "net_growth": 183,
        "active_mrr": 155_364.0,
    },
    revenue={
        "subscription_revenue": 41_800.0,
        "checkout_revenue": 23_900.0,
        "recurring_revenue": 17_900.0,
    },
)


def test_retention_block_ties_additions_reductions_and_net_growth() -> None:
    lines = brief.retention_block(_subs(), PRIOR_SUBS, {"recurring_revenue": 19_400.0})
    assert lines[0] == f"{G} **Active subscribers**  8,807 · +2.2% vs prior week"
    assert lines[1] == f"{G} **Subscriber additions**  311 · +7.2% vs prior week"
    # Reductions read the other way: 9.3% MORE churn is unfavourable.
    assert lines[2] == f"{Y} **Subscriber reductions**  117 · +9.3% vs prior week"
    assert lines[3] == f"{G} **Net subscriber growth**  +194 · prior week +183"
    assert lines[4] == (
        f"{Y} **Subscription revenue**  $43.0K — checkout $24.7K · recurring $18.3K vs "
        f"$19.4K goal (-5.7%) · +2.9% vs prior week"
    )
    assert lines[5] == f"{G} **Active MRR**  $158.5K · +2.0% vs prior week"
    assert len(lines) == 6


def test_negative_net_subscriber_growth_is_always_red() -> None:
    """The book shrank. However small the move, that is not a watch item."""
    shrinking = _subs(subscribers={"additions": 100, "reductions": 140, "net_growth": -40})
    line = brief.retention_block(shrinking, PRIOR_SUBS, {})[3]
    assert line == f"{R} **Net subscriber growth**  -40 · prior week +183"
    # And a big drop in churn is green even though "fewer" is a negative %.
    better = _subs(subscribers={"reductions": 80})
    assert brief.retention_block(better, PRIOR_SUBS, {})[2].startswith(G)


def test_retention_block_says_once_when_there_is_no_point_in_time_history() -> None:
    blind = _subs(point_in_time_available=False, note="history starts 2026-06-11")
    lines = brief.retention_block(blind, PRIOR_SUBS, {})
    assert lines == [f"{W} **Subscribers**  n/a — history starts 2026-06-11"]


def test_month_block_scores_the_month_and_flags_spend_room() -> None:
    lines = brief.month_block(_pacing())
    assert lines[0] == "**September — day 13 of 30**"
    # The month speaks the week's vocabulary: one number never has two names.
    assert lines[1].startswith(
        f"{Y} **Total revenue**  $123.1K vs $134.2K goal (-8.3%) · run-rate"
    )
    assert lines[2] == f"{Y} **New customer ROAS**  1.57x vs 1.60x goal (-1.9%) · NC AOV $70"
    # 12.1% under goal is inside the 15% watch band, so no commentary is added.
    assert lines[3] == f"{G} **Ad spend**  $40.0K vs $45.5K goal (-12.1%)"
    assert lines[4] == f"{Y} **New customer CAC**  $45 vs $42 goal (+5.4%)"
    # 13.9% under goal: inside the widened band, so watch rather than escalate.
    assert lines[5] == f"{Y} **NC demand**  $62.7K vs $72.8K goal (-13.9%)"
    assert lines[6].startswith(f"{R} **New customers**  892 vs 1,071 goal (-16.7%)")
    assert (
        lines[7]
        == "To go $193.0K over 17 days ($11.4K/day) · vs LY +129.0% · vs same days last month +0.6%"
    )


def test_month_block_flags_spend_room_only_past_the_watch_band() -> None:
    """Under-spending is a decision, not a miss — but past 15% it is money on
    the table, and the month block says so."""
    p = _pacing(mtd={**_pacing()["summary"]["mtd"], "spend": 30_000.0})
    assert brief.month_block(p)[3].endswith("· room to scale if efficiency holds")


def test_month_block_ahead_of_forecast_never_prints_a_negative_to_go() -> None:
    p = _pacing(
        mtd={**_pacing()["summary"]["mtd"], "demand": 320_000.0},
        to_go={"demand": -3_900.0},
        required_daily_average={"demand": -780.0},
    )
    tail = brief.month_block(p)[-1]
    assert tail.startswith("Already $3,900 past the month forecast with 17 days left")
    assert "$-" not in tail


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
    assert notes[1].startswith("No goal loaded for this month (missing_month)")


def test_week_without_a_plan_says_why() -> None:
    plan = brief.plan_for(pacing_fx._targets(), date(2026, 7, 27), date(2026, 8, 2))
    notes = brief.week_plan_note(plan, date(2026, 7, 27), date(2026, 8, 2))
    assert notes == [
        "No goal for Mon Jul 27–Sun Aug 2: the pacing sheet has no forecast for every day in "
        "it (a month's tab is missing from the config), so those lines show actuals only."
    ]
    assert brief.week_plan_note({"demand": 1.0}, date(2026, 8, 3), date(2026, 8, 9)) == []


def test_footer_shows_both_dates_when_the_month_runs_past_the_window() -> None:
    p = _pacing()
    f = brief.footer(p, date(2026, 9, 6))
    assert "data through Sun Sep 6 (month through Sun Sep 13)" in f
    assert '"September 2026" tab' in f and "refreshed 2026-09-11" in f
    assert "total revenue = net sales + shipping" in f
    # The subscriber definitions ride along only where subscriber lines do.
    assert "point-in-time" not in f
    assert "point-in-time" in brief.footer(p, date(2026, 9, 6), with_subscribers=True)
    assert "(month through" not in brief.footer(p, date(2026, 9, 13))


def test_sum_rows_recomputes_ratios_from_sums() -> None:
    rows = [
        {
            "spend": 100.0,
            "orders": 4,
            "new_customers": 2,
            "demand": 300.0,
            "nc_orders": 2,
            "nc_demand": 120.0,
            "gross_sales": 320.0,
        },
        {"spend": 50.0, "orders": 1, "new_customers": 0, "demand": 0.0, "gross_sales": 0.0},
    ]
    t = brief._sum_rows(rows)
    assert (t["spend"], t["orders"], t["demand"]) == (150.0, 5.0, 300.0)
    assert t["nc_roas"] == pytest.approx(0.8) and t["nc_aov"] == pytest.approx(60.0)
    # MER and AOV are on demand (the sheet's definition), never on list-price gross.
    assert t["mer"] == pytest.approx(2.0) and t["cac"] == pytest.approx(75.0)
    assert t["nc_share"] == pytest.approx(0.4)
    empty = brief._sum_rows([])
    assert empty["mer"] is None and empty["nc_roas"] is None  # no spend: no ratio, never a zero


def test_daily_table_has_a_row_per_day_and_a_total() -> None:
    t = pacing_fx._targets()
    days = {
        date(2026, 8, 3): {"demand": 30.0, "spend": 50.0, "new_customers": 1, "blended_cac": 50.0},
        date(2026, 8, 4): {"demand": 0.0, "spend": 0.0, "new_customers": 0, "blended_cac": None},
    }
    out = brief.daily_table(date(2026, 8, 3), date(2026, 8, 4), days, None, t, title="Days")
    body = out.split("```")[1].strip().splitlines()
    assert body[0].split() == ["Day", "Revenue", "vs", "goal", "Spend", "NCs", "CAC"]  # no LY
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


def _subpulse_payload(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "mode": "subpulse",
        "today": date(2026, 9, 17),
        "wtd": (date(2026, 9, 14), date(2026, 9, 16)),
        "mtd": (date(2026, 9, 1), date(2026, 9, 16)),
        "since_monday": _subs(
            subscribers={"active": 8_864, "additions": 163, "reductions": 70, "net_growth": 93}
        ),
        "month_subs": _subs(
            subscribers={"active": 8_864, "additions": 763, "reductions": 306, "net_growth": 457}
        ),
    }
    d.update(over)
    return d


def test_subpulse_is_three_subscriber_lines_and_their_move_since_monday() -> None:
    out = brief.render_subpulse(_subpulse_payload())
    lines = out["main"].splitlines()
    assert lines[0] == "🔁 **DTC — mid-week subscriber pulse** (Mon Sep 14–Wed Sep 16)"
    assert lines[2] == "**Active subscribers**  8,864 (+93 since Monday)"
    assert lines[3] == "**New subscribers**  763 so far this month (+163 since Monday)"
    assert lines[4] == "**Subscriber reductions**  306 so far this month (+70 since Monday)"
    # No scorecard, no month block, no threaded tables: this is the whole post.
    assert "vs goal" not in out["main"] and out["replies"] == []
    assert "month to date Sep 1–Sep 16" in lines[-1]


def test_subpulse_with_one_closed_day_says_so() -> None:
    d = _subpulse_payload(wtd=(date(2026, 9, 14), date(2026, 9, 14)))
    assert "Only one day of the week has closed" in brief.render_subpulse(d)["main"]


def test_subpulse_without_point_in_time_history_says_so_instead_of_guessing() -> None:
    d = _subpulse_payload(
        since_monday=_subs(point_in_time_available=False, note="history starts 2026-06-11")
    )
    main = brief.render_subpulse(d)["main"]
    assert "Subscriber counts unavailable — history starts 2026-06-11" in main
    assert "Active subscribers" not in main


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


# The order fixtures predate `purchase_type` / `order_source` mattering, so the
# subscription split is layered on here rather than in test_pacing (whose
# expectations are about demand, not subscriptions): o1 is the checkout order
# that STARTS customer 1's subscription, r1 is a Loop renewal inside the same
# week, and everything else is one-time.
SUBSCRIPTION_ORDERS = {"o1": "web", "r1": "subscription_contract_checkout_one"}
ORDER_LINES = [
    {
        **r,
        "order_source": SUBSCRIPTION_ORDERS.get(r["order_id"], "web"),
        "purchase_type": "Subscription" if r["order_id"] in SUBSCRIPTION_ORDERS else "One Time",
    }
    for r in pacing_fx.ORDER_LINES
] + [
    {
        "order_date_ct": "2026-07-28",
        "order_id": "r1",
        "customer_id": 4,
        "channel_bucket": "core_d2c",
        "is_paid_order": True,
        "order_subtotal": 20.0,
        "order_shipping": 0.0,
        "gross_line": 20.0,
        "net_line": 20.0,
        "order_source": "subscription_contract_checkout_one",
        "purchase_type": "Subscription",
    }
]

# dtc_subscriptions is SCD2 HISTORY: one row per version of a contract, and the
# tool reads the version whose valid_from is the latest before the instant it
# means. Three contracts, chosen so the week of Jul 27–Aug 2 has one of each:
#
#   s1 (customer 4)  active throughout — in both the opening and closing sets
#   s2 (customer 1)  starts Aug 1      — an ADDITION
#   s3 (customer 2)  cancelled Jul 29  — a REDUCTION (two row versions)
#
# so additions 1, reductions 1, net 0, and active holds at 2. MRR at the close
# is s1 (30 + 5 delivery, monthly) + s2 (20 every 2 months) = 45.
SUBSCRIPTIONS = [
    {
        "subscription_id": "s1",
        "customer_id": 4,
        "status": "ACTIVE",
        "subscription_created_date": "2026-07-10",
        "cancelled_date": None,
        "recurring_price": 30.0,
        "recurring_delivery": 5.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 1,
        "valid_from": "2026-07-01 12:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "s2",
        "customer_id": 1,
        "status": "ACTIVE",
        "subscription_created_date": "2026-08-01",
        "cancelled_date": None,
        "recurring_price": 20.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 2,
        "valid_from": "2026-08-01 12:00:00+00",
        "is_current": True,
    },
    {
        "subscription_id": "s3",
        "customer_id": 2,
        "status": "ACTIVE",
        "subscription_created_date": "2026-07-05",
        "cancelled_date": None,
        "recurring_price": 60.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 3,
        "valid_from": "2026-07-05 12:00:00+00",
        "is_current": False,
    },
    {
        "subscription_id": "s3",
        "customer_id": 2,
        "status": "CANCELLED",
        "subscription_created_date": "2026-07-05",
        "cancelled_date": "2026-07-29",
        "recurring_price": 60.0,
        "recurring_delivery": 0.0,
        "billing_interval": "MONTH",
        "billing_interval_count": 3,
        "valid_from": "2026-07-29 12:00:00+00",
        "is_current": True,
    },
]


def _tables() -> dict[str, Any]:
    return {
        **pacing_fx._tables(),
        "dtc_order_lines": ORDER_LINES,
        "dtc_revenue_lines": REVENUE_LINES,
        "dtc_subscriptions": SUBSCRIPTIONS,
    }


@pytest.mark.bq
async def test_weekly_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)  # Thu 2026-08-06
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("weekly", warehouse=wh, targets=pacing_fx._targets())
    assert d["week"] == (date(2026, 7, 27), date(2026, 8, 2))
    out = brief.render(d)
    main = out["main"]
    # Week of Jul 27: p1 (30) on Jul 30 + r1 (20) on Jul 28 + o1 (40 + 5 shipping,
    # counted once) on Aug 1 -> 95. July has no tab in the fixture targets, so the
    # week has NO goal: neutral lights, no percentages.
    assert main.startswith(
        "🛒 **DTC — week of Mon Jul 27–Sun Aug 2**\n\n**Revenue & Efficiency**\n"
        f"{W} **Total revenue**  $95"
    )
    # o1 (cust 1, first order Aug 1) is the week's only first order: NC demand 45 on spend 100.
    assert f"{W} **New customer ROAS**  0.45x · NC AOV $45" in main
    # Acquisition: that one new customer arrived on a subscription, so 100%.
    assert f"{W} **Subscriber take rate**  100.0%" in main
    # Retention: the three fixture contracts, and the identity that ties them.
    assert f"{G} **Active subscribers**  2 · +0.0% vs prior week" in main
    # A zero prior week has no percentage, so the line shows the level it moved
    # from — and the dot still reads the direction: one more addition is growth,
    # one more cancellation is not.
    assert f"{G} **Subscriber additions**  1 · prior week 0" in main
    assert f"{R} **Subscriber reductions**  1 · prior week 0" in main
    assert f"{G} **Net subscriber growth**  +0 · prior week +0" in main
    # Subscription revenue splits by order source: o1 is the checkout that started
    # a subscription, r1 is the Loop renewal.
    assert f"{G} **Subscription revenue**  $65 — checkout $45 · recurring $20 · prior week $0" in main
    # s3 cancelling took its $20/month out of the book.
    assert f"{R} **Active MRR**  $45 · -18.2% vs prior week" in main
    assert "Blended MER 0.95x" in main  # 95 demand / 100 spend (Aug 1)
    assert "**August — day 5 of 31**" in main
    assert f"{G} **Total revenue**  $75 vs $50 goal (+50.0%)" in main
    assert (
        f"{G} **New customer ROAS**  0.50x vs 0.40x goal (+25.0%) · NC AOV $38" in main
    )  # month, 2 first orders
    assert f"{G} **NC demand**  $75 vs $40 goal (+87.5%)" in main
    assert "data through Sun Aug 2 (month through Wed Aug 5)" in main
    assert "• No goal for Mon Jul 27–Sun Aug 2" in main
    assert "sets no goal for the subscriber lines" in main
    assert len(out["replies"]) == 2
    days, trend = out["replies"]
    assert days.startswith("**Day by day — Mon Jul 27–Sun Aug 2**")
    assert "Thu 30" in days and "Total" in days
    assert trend.startswith("**") and "Jul 27" in trend


@pytest.mark.bq
async def test_subpulse_brief_end_to_end(fixture_warehouse: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(dtc, "today_reporting", lambda: TODAY)  # Thu 2026-08-06
    wh = fixture_warehouse(**_tables())
    d = await brief.gather("subpulse", warehouse=wh, targets=pacing_fx._targets())
    assert d["wtd"] == (date(2026, 8, 3), date(2026, 8, 5))
    assert d["mtd"] == (date(2026, 8, 1), date(2026, 8, 5))
    main = brief.render(d)["main"]
    # Month to date (Aug 1-5): s2 started Aug 1, so customer 1 joined and the book
    # stands at 2 (s1's customer 4 and s2's customer 1 — s3 cancelled in July).
    assert main.startswith("🔁 **DTC — mid-week subscriber pulse** (Mon Aug 3–Wed Aug 5)")
    assert "**Active subscribers**  2 (+0 since Monday)" in main
    assert "**New subscribers**  1 so far this month (+0 since Monday)" in main
    assert "**Subscriber reductions**  0 so far this month (+0 since Monday)" in main
    assert brief.render(d)["replies"] == []


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
        f"🛒 **DTC — August 2026 recap** — **miss**\n\n**Month vs forecast**\n"
        f"{R} **Total revenue**  $75 vs $310 goal (-75.8%)"
    )
    # Same line order as the month block: NC demand, then New customers.
    assert (
        f"{R} **NC demand**  $75 vs $248 goal (-69.8%)\n{R} **New customers**  2 vs 31 goal (-93.5%)"
        in main
    )
    assert f"{G} **Ad spend**  $150 vs $620 goal (-75.8%) · under goal" in main
    assert f"{G} **New customer ROAS**  0.50x vs 0.40x goal (+25.0%) · NC AOV $38" in main
    # Certified net revenue for the month: o1 (30 + 10) + o2 (25) = 65 vs demand 75.
    assert "**Net revenue (certified, after refunds)** $65 · -13.3% vs total revenue" in main
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
    # $110: the July orders plus r1, the Loop renewal added to the fixtures.
    assert main.startswith(
        f"🛒 **DTC — July 2026 recap**\n\n**Month vs forecast**\n{W} **Total revenue**  $110\n"
        f"{W} **New customer ROAS**"
    )
    assert "No goal loaded for this month" in main


def test_targets_file_carries_forecasts_only() -> None:
    """The sheet is the source of targets and nothing else: no actual column is read."""
    assert all(f.startswith("forecast_") for f in pt.FIELD_MAP)
    t = pt.load_targets(ROOT / "config" / "dtc_pacing_targets.json")
    for m in t.months.values():
        for day in m.days.values():
            assert all(k.startswith("forecast_") for k in day.values)
