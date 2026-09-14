"""Tests for the Target POS Slack brief (scripts/pos_brief.py).

Everything here is pure — no BigQuery. The queries are covered by running the
script against live data; what is pinned here is the interpretation layer, which
is where the brief has actually gone wrong: a short week silently diluting a
trailing average, a per-door velocity invented for an online-only SKU, and a
record week that failed to announce itself over a 1e-10 float difference.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("pos_brief", ROOT / "scripts" / "pos_brief.py")
assert _spec and _spec.loader
pos_brief = importlib.util.module_from_spec(_spec)
sys.modules["pos_brief"] = pos_brief
_spec.loader.exec_module(pos_brief)

# The date Target's sales feed switched from weekly rows to daily rows.
CUTOVER = date(2026, 5, 6)


def _week(week_end: date, days: int, **kw) -> SimpleNamespace:
    return SimpleNamespace(week_end=week_end, days=days, **kw)


# ---------- week completeness ----------


def test_pre_cutover_week_is_complete_with_a_single_saturday_row() -> None:
    """Before the cutover the whole week arrives as one Saturday-stamped row,
    so demanding seven dated days would reject all of history."""
    assert pos_brief._complete(_week(date(2026, 5, 2), days=1), CUTOVER)


def test_post_cutover_week_needs_seven_dated_days() -> None:
    assert pos_brief._complete(_week(date(2026, 6, 6), days=7), CUTOVER)
    assert not pos_brief._complete(_week(date(2026, 6, 6), days=6), CUTOVER)


def test_cutover_week_is_rejected_as_short() -> None:
    """w/e 2026-05-09 holds only Wed-Sat (the weekly feed had stopped, the daily
    feed had not started). It reads $183.9K against neighbours near $330K, and
    averaging it in understated the 13-week average by ~$11K/wk."""
    assert not pos_brief._complete(_week(date(2026, 5, 9), days=4), CUTOVER)


def test_short_week_is_excluded_from_a_trailing_average() -> None:
    raw = [
        _week(date(2026, 5, 23), 7, amt=331_515.0),
        _week(date(2026, 5, 16), 7, amt=270_649.0),
        _week(date(2026, 5, 9), 4, amt=183_923.0),  # short — must not dilute
        _week(date(2026, 5, 2), 1, amt=352_228.0),
    ]
    kept = [r for r in raw if pos_brief._complete(r, CUTOVER)]
    assert [r.week_end for r in kept] == [
        date(2026, 5, 23),
        date(2026, 5, 16),
        date(2026, 5, 2),
    ]
    assert sum(r.amt for r in kept) / len(kept) == pytest.approx(318_130.67, abs=0.01)


# ---------- week labels ----------


@pytest.mark.parametrize(
    ("week_end", "label"),
    [
        # The four weeks of the SepW2'26 report's own summary table, each tied
        # to its BigQuery dollar total — the pins that fix this convention.
        (date(2026, 9, 12), "Sep W2 '26"),
        (date(2026, 9, 5), "Sep W1 '26"),  # fiscal September opens Sun Aug 30
        (date(2026, 8, 29), "Aug W4 '26"),
        (date(2026, 8, 22), "Aug W3 '26"),
        (date(2026, 8, 1), "Jul W4 '26"),  # the JulW4'26 report, same week
        (date(2026, 8, 8), "Aug W1 '26"),
        (date(2026, 7, 4), "Jun W5 '26"),  # fiscal June is a 5-week month
        (date(2026, 10, 3), "Sep W5 '26"),  # so is fiscal September
        (date(2026, 1, 3), "Dec W5 '25"),  # fiscal December runs into January
        (date(2027, 1, 30), "Jan W4 '27"),  # last week of FY2026
        (date(2029, 2, 3), "Jan W5 '29"),  # the 53rd week of FY2028
    ],
)
def test_fiscal_label_follows_targets_454_calendar(week_end: date, label: str) -> None:
    """KMG numbers weeks inside Target's 4-5-4 fiscal month.

    Counting Sundays in the CALENDAR month agrees for most of the year and then
    slips a week at every 5-week month: it called w/e Sep 5 2026 "Aug W5" and
    every later September week one number low, so the Monday brief and the
    report it is read beside disagreed on which week it was.
    """
    assert pos_brief._fiscal_label(week_end) == label


def test_fiscal_months_hold_the_454_week_counts() -> None:
    """FY2026 must come out as 52 weeks split 4-5-4 per quarter — the shape the
    SepW2'26 promo recap shows, with a Jun W5, a Sep W5 and a Dec W5 and every
    other month stopping at W4."""
    counts: Counter[str] = Counter()
    week_end = pos_brief._fy_start(2026) + timedelta(days=6)
    while week_end <= date(2027, 1, 30):
        counts[pos_brief._fiscal_label(week_end).split()[0]] += 1
        week_end += timedelta(days=7)
    assert sum(counts.values()) == 52
    assert [counts[m] for m in ("Feb", "Mar", "Apr")] == [4, 5, 4]
    assert [counts[m] for m in ("May", "Jun", "Jul")] == [4, 5, 4]
    assert [counts[m] for m in ("Aug", "Sep", "Oct")] == [4, 5, 4]
    assert [counts[m] for m in ("Nov", "Dec", "Jan")] == [4, 5, 4]


@pytest.mark.parametrize(
    ("fy", "start"),
    [
        (2025, date(2025, 2, 2)),  # Feb 1 is a Saturday — the year starts after it
        (2026, date(2026, 2, 1)),  # Feb 1 is itself the Sunday
        (2027, date(2027, 1, 31)),  # nearest Sunday falls back into January
        (2028, date(2028, 1, 30)),
    ],
)
def test_fiscal_year_starts_on_the_sunday_nearest_feb_1(fy: int, start: date) -> None:
    assert pos_brief._fy_start(fy) == start


@pytest.mark.parametrize(
    ("sales_through", "week_end"),
    [
        (date(2026, 8, 15), date(2026, 8, 15)),  # Sat, caught up — report THAT week
        (date(2026, 8, 16), date(2026, 8, 15)),  # Sun, one day into the new week
        (date(2026, 8, 17), date(2026, 8, 15)),  # Mon, the routine's own slot
        (date(2026, 8, 20), date(2026, 8, 15)),  # Thu, mid-week
        (date(2026, 8, 21), date(2026, 8, 15)),  # Fri, still mid-week
    ],
)
def test_latest_week_end_does_not_skip_a_closed_week(sales_through: date, week_end: date) -> None:
    """When the feed catches up through a Saturday, that week has closed. Backing
    off a further week made the Monday brief a week stale — and would have
    reposted the prior week's brief verbatim."""
    assert pos_brief._latest_week_end(sales_through) == week_end


# ---------- percentage formatting ----------


def test_pct_never_prints_negative_zero() -> None:
    """A -0.04% move rounds to zero and used to render as "-0.0%", which reads
    as a defect to anyone scanning the SKU table."""
    assert pos_brief._pct(999.6, 1000.0) == "+0.0%"
    assert pos_brief._pct(1000.4, 1000.0) == "+0.0%"


def test_pct_handles_missing_and_zero_baselines() -> None:
    assert pos_brief._pct(100.0, None) == "n/a"
    assert pos_brief._pct(100.0, 0) == "n/a"
    assert pos_brief._pct(None, 100.0) == "n/a"
    assert pos_brief._pct(110.0, 100.0) == "+10.0%"
    assert pos_brief._pct(90.0, 100.0) == "-10.0%"


# ---------- KMG annotation ----------


CFG = {
    "source": "KMG JulW4'26 POS report",
    "as_of": "2026-08-03",
    "assortment": {
        "003-02-5627": {"name": "Disinf Refill 180ct Var", "goal_pspw": 51.96, "pog_doors": 1598},
        "007-07-1897": {
            "name": "Baby Kit - Seafoam Grn",
            "goal_pspw": None,
            "pog_doors": 1387,
            "limited_time": True,
        },
        # Newly listed: KMG publishes the goal before the door count.
        "007-08-5892": {"name": "Mini Disp w/LM 40ct Peri", "goal_pspw": 7.91},
    },
    "excluded": {
        "003-02-7872": {"name": "Dispenser - Black", "pog_doors": None, "reason": "online-only"},
    },
}


def _sku(dpci, amt=1000.0, units=100.0, doors=999, descr="Biom Refillable Plant-Ba"):
    return SimpleNamespace(dpci=dpci, amt=amt, units=units, doors=doors, descr=descr)


def test_kmg_pog_doors_beat_the_inventory_door_count() -> None:
    """Inventory counts every store holding stock, including ones with no POG
    authorization. Using 1,726 instead of KMG's 1,598 understates $PSPW."""
    s = pos_brief._annotate([_sku("003-02-5627", amt=43_550.0, doors=1726)], CFG)[0]
    assert s.doors_pog == 1598
    assert s.pspw == pytest.approx(27.25, abs=0.01)
    assert s.pct_goal == pytest.approx(52.4, abs=0.1)
    assert s.name == "Disinf Refill 180ct Var"
    assert s.in_assortment and s.known


def test_online_only_sku_gets_no_per_door_velocity() -> None:
    """An explicit null pog_doors means no planogram presence. Falling back to
    the few stores holding stock would print a $PSPW for an item that has none."""
    s = pos_brief._annotate([_sku("003-02-7872", amt=67.0, doors=29)], CFG)[0]
    assert s.doors_pog is None
    assert s.pspw is None and s.upspw is None and s.pct_goal is None
    assert s.known and not s.in_assortment


def test_unknown_dpci_falls_back_to_inventory_doors() -> None:
    """A genuinely new item has no KMG entry yet, so the inventory door count is
    the only denominator available — and it must still be flagged as unknown."""
    s = pos_brief._annotate([_sku("999-99-9999", amt=600.0, doors=50)], CFG)[0]
    assert s.doors_pog == 50
    assert s.pspw == pytest.approx(12.0)
    assert not s.known and not s.in_assortment
    assert s.name == "Biom Refillable Plant-Ba"  # falls back to the feed's text


def test_goaled_sku_without_a_published_goal_has_velocity_but_no_attainment() -> None:
    s = pos_brief._annotate([_sku("007-07-1897", amt=18_119.0)], CFG)[0]
    assert s.in_assortment
    assert s.pspw == pytest.approx(13.06, abs=0.01)
    assert s.goal is None and s.pct_goal is None


def test_new_item_with_a_goal_but_no_door_count_is_still_scored() -> None:
    """KMG published $PSPW goals for the two Little Mess minis in SepW2'26
    without door counts. A missing key is NOT a null: nulling it would leave a
    goaled item with no $PSPW at all, so the inventory door count stands in and
    is marked as an estimate."""
    s = pos_brief._annotate([_sku("007-08-5892", amt=19_745.0, doors=1053)], CFG)[0]
    assert s.doors_pog == 1053 and s.doors_estimated
    assert s.pspw == pytest.approx(18.75, abs=0.01)
    assert s.pct_goal == pytest.approx(237.0, abs=0.5)
    assert s.in_assortment and s.known


def test_a_kmg_door_count_is_never_marked_as_an_estimate() -> None:
    s = pos_brief._annotate([_sku("003-02-5627", amt=43_550.0, doors=1726)], CFG)[0]
    assert not s.doors_estimated
    # Nor is a SKU KMG authorizes no doors for: there is no denominator to flag.
    assert not pos_brief._annotate([_sku("003-02-7872", doors=29)], CFG)[0].doors_estimated


# ---------- the shipped config ----------


def test_shipped_goals_file_is_well_formed() -> None:
    cfg = json.loads((ROOT / "config" / "pspw_goals.json").read_text())
    assert cfg["as_of"] and cfg["source"]
    assert not set(cfg["assortment"]) & set(cfg["excluded"]), "a DPCI cannot be both"
    for dpci, meta in cfg["assortment"].items():
        assert meta["name"], dpci
        assert meta["goal_pspw"] is None or meta["goal_pspw"] > 0, dpci
        if "pog_doors" in meta:
            assert isinstance(meta["pog_doors"], int) and meta["pog_doors"] > 0, dpci
        else:
            # Omitting the key buys the inventory-door fallback, which only
            # earns its keep for an item that has a goal to be scored against.
            assert meta["goal_pspw"], f"{dpci}: no doors and no goal"
    for dpci, meta in cfg["excluded"].items():
        assert meta["name"] and meta["reason"], dpci


# ---------- end-to-end render ----------


def _render_input(cur_amt: float, record_high: float) -> dict:
    """Smallest input render_weekly accepts, with one goaled SKU."""
    weeks = [date(2026, 8, 1), date(2026, 7, 25), date(2026, 7, 18), date(2026, 7, 11)]
    series = [
        SimpleNamespace(
            week_end=w,
            days=7,
            amt=cur_amt - i * 10_000,
            units=39_193 - i * 1_000,
            promo_amt=(cur_amt - i * 10_000) * 0.5,
            online_amt=(cur_amt - i * 10_000) * 0.3,
            doors=1814,
        )
        for i, w in enumerate(weeks)
    ]
    skus = pos_brief._annotate([_sku("003-02-5627", amt=43_550.0, units=4_004.0, doors=1726)], CFG)
    for s in skus:
        s.prev_amt, s.eoh_ow, s.wip, s.oos = 41_700.0, 52_184.0, 99.9, 0.06
        s.prev_oos, s.prev_eoh_ow = 0.05, 53_000.0
    inv = {
        w: SimpleNamespace(eoh_ow=493_924.0 + i * 5_000, wip=99.5, oos=0.49)
        for i, w in enumerate(weeks)
    }
    return {
        "week_end": weeks[0],
        "series": series,
        "dropped": [SimpleNamespace(week_end=date(2026, 5, 9), days=4)],
        "record_high": record_high,
        "inventory": inv,
        "skus": skus,
        "mix": [SimpleNamespace(grp="Baby", amt=107_600.0)],
        "goals": CFG,
        "pspw_by_week": dict.fromkeys(weeks, 255.61),
        "upspw_by_week": dict.fromkeys(weeks, 28.6),
        "sales_through": date(2026, 8, 9),
        "inv_through": date(2026, 8, 9),
    }


def test_record_week_survives_float_summation_order() -> None:
    """The week total and the all-time high are summed by different queries, so
    they disagree in the 10th decimal even for the same week. An exact >=
    comparison silently dropped "Record week." from a genuine record."""
    amt = 391_030.3600000058
    out = pos_brief.render_weekly(_render_input(amt, record_high=391_030.3600000059))
    assert out["main"].startswith("🎯")
    assert "**Record week.**" in out["main"]


def test_a_week_below_the_high_is_not_called_a_record() -> None:
    out = pos_brief.render_weekly(_render_input(370_753.0, record_high=391_030.0))
    assert "Record week" not in out["main"]


def test_emphasis_is_standard_markdown_not_slack_mrkdwn() -> None:
    """The delivery path parses standard markdown, where a single asterisk is
    italic. Emitting Slack's native `*bold*` posts every header as italics."""
    main = pos_brief.render_weekly(_render_input(391_030.0, record_high=391_030.0))["main"]
    for heading in ("**Target POS", "**What's working**", "**What to watch**"):
        assert heading in main
    stripped = main.replace("**", "")
    assert "*" not in stripped, "a single-asterisk emphasis marker survived"


def test_ungoaled_sku_still_reaches_the_in_stock_flags() -> None:
    """HS Go-Pack 20ct carries no published $PSPW goal, so scoping the flags to
    the goaled assortment hid it breaching the 5.0% OOS goal at 6.4% on $5.1K of
    sales. Lacking a goal keeps a SKU out of the goal math, not out of the
    in-stock flags — while the de-listed tail stays suppressed on volume."""
    d = _render_input(391_030.0, record_high=391_030.0)
    ungoaled = pos_brief._annotate([_sku("003-02-7872", amt=5_143.0, units=2_174.0)], CFG)[0]
    ungoaled.prev_amt, ungoaled.eoh_ow = 5_050.0, 6_678.0
    ungoaled.wip, ungoaled.oos = 96.0, 4.0  # under the goal → trailing roll-up
    ungoaled.prev_oos, ungoaled.prev_eoh_ow = 2.6, 8_567.0

    delisted = pos_brief._annotate([_sku("999-99-9999", amt=28.0, units=2.0, doors=3)], CFG)[0]
    delisted.prev_amt, delisted.eoh_ow = 20.0, 44.0
    delisted.wip, delisted.oos = 20.0, 80.0
    delisted.prev_oos, delisted.prev_eoh_ow = 75.0, 49.0

    d["skus"] = [*d["skus"], ungoaled, delisted]
    flag_line = next(
        ln for ln in pos_brief.render_weekly(d)["main"].splitlines() if "Highest OOS" in ln
    )
    assert "Dispenser - Black 4.0%" in flag_line
    assert "80.0%" not in flag_line


def test_sku_past_the_in_stock_goal_gets_its_own_callout_with_the_trend() -> None:
    """A developing stockout reads as routine when it is the third name in a
    trailing roll-up. HS Go-Pack went 0% → 2.6% → 6.4% → 18.9% over four weeks
    while cover halved; that belongs at the top of "What to watch", with last
    week's rate next to it so a blip is distinguishable from a trend."""
    d = _render_input(391_030.0, record_high=391_030.0)
    breaching = pos_brief._annotate([_sku("003-02-7872", amt=5_026.0, units=1_864.0)], CFG)[0]
    breaching.prev_amt, breaching.eoh_ow = 5_143.0, 4_694.0
    breaching.wip, breaching.oos = 81.1, 18.93
    breaching.prev_oos, breaching.prev_eoh_ow = 6.42, 6_678.0
    d["skus"] = [*d["skus"], breaching]

    watch = pos_brief.render_weekly(d)["main"].split("**What to watch**")[1]
    callout = next(ln for ln in watch.splitlines() if "past the" in ln)
    assert "Dispenser - Black" in callout
    assert "OOS 18.9%" in callout and "up from 6.4% last week" in callout
    assert "6,678 → 4,694 units" in callout
    # It leads the section, and is not also repeated in the roll-up below.
    assert watch.strip().splitlines()[0] == callout.strip()
    assert "Highest OOS" not in watch or "Dispenser - Black" not in watch.split("Highest OOS")[1]


def test_weekly_flags_an_implausible_full_oos_as_a_feed_gap_not_a_breach() -> None:
    """Same feed-dropout scenario as the pulse, in the weekly renderer: a SKU
    that sold units this week cannot also be reported as universally OOS."""
    d = _render_input(391_030.0, record_high=391_030.0)
    gap = pos_brief._annotate([_sku("003-02-7872", amt=5_026.0, units=113.0)], CFG)[0]
    gap.prev_amt, gap.eoh_ow = 5_143.0, 1_592.0
    gap.wip, gap.oos = 0.0, 100.0
    gap.prev_oos, gap.prev_eoh_ow = 41.7, 1_818.0
    d["skus"] = [*d["skus"], gap]

    main = pos_brief.render_weekly(d)["main"]
    assert "🟠" in main
    line = next(ln for ln in main.splitlines() if "inventory feed gap" in ln)
    assert "Dispenser - Black" in line and "OOS reads 100.0%" in line
    watch = main.split("**What to watch**")[1]
    assert "past the" not in watch or "Dispenser - Black" not in watch.split("past the")[1]


def test_weekly_reports_a_limited_time_item_as_sell_through_not_a_breach() -> None:
    """Same limited-time rule in the weekly renderer: an LTO selling down is
    the plan, so it must not land in the breach callouts or the Highest-OOS
    roll-up alongside items that are genuinely missing the goal."""
    d = _render_input(391_030.0, record_high=391_030.0)
    lto = pos_brief._annotate([_sku("007-07-1897", amt=18_119.0, units=743.0)], CFG)[0]
    lto.prev_amt, lto.eoh_ow = 17_000.0, 8_108.0
    lto.wip, lto.oos = 94.4, 5.6
    lto.prev_oos, lto.prev_eoh_ow = 5.3, 8_581.0
    d["skus"] = [*d["skus"], lto]

    watch = pos_brief.render_weekly(d)["main"].split("**What to watch**")[1]
    sell = next(ln for ln in watch.splitlines() if "Baby Kit" in ln and "cover left" in ln)
    assert "743 units this week" in sell and "8,108 units of cover left" in sell
    assert "Baby Kit - Seafoam Grn is past the" not in watch
    assert "Highest OOS" not in watch or "Baby Kit" not in watch.split("Highest OOS")[1]


def test_shipped_goals_file_marks_the_limited_time_items() -> None:
    """Pinned because the flag is what keeps an intended sell-out from paging
    leadership: the HS 20ct go-pack and the two LGR+PUR baby kits."""
    cfg = json.loads((ROOT / "config" / "pspw_goals.json").read_text())
    ref = {**cfg["assortment"], **cfg["excluded"]}
    assert {d for d, m in ref.items() if m.get("limited_time")} == {
        "253-04-9259",  # HS Go-Pack 20ct Santal
        "007-07-1897",  # Baby Kit - Seafoam Grn
        "007-07-5306",  # Baby Kit - Lilac
    }


def test_render_reports_the_short_week_it_dropped() -> None:
    """Excluding a week from the averages is a judgement call, so it is stated
    in the footer rather than applied silently."""
    out = pos_brief.render_weekly(_render_input(391_030.0, record_high=391_030.0))
    assert "w/e 2026-05-09 (4/7 days)" in out["main"]
    assert "quantity-weighted" in out["main"]
    assert len(out["replies"]) == 2


def _with_extra_sku(sku) -> dict:
    d = _render_input(391_030.0, record_high=391_030.0)
    d["skus"] = [*d["skus"], sku]
    return d


def test_render_says_which_doors_are_estimated() -> None:
    """A % to goal built on an inventory door count is not one KMG could
    reproduce, and a newly-listed item is where a reader is least likely to
    notice. The footer names it and the SKU table marks the denominator."""
    new = pos_brief._annotate([_sku("007-08-5892", amt=19_745.0, units=1_345.0, doors=1053)], CFG)[
        0
    ]
    new.prev_amt, new.eoh_ow, new.wip, new.oos = 18_754.0, 3_314.0, 86.5, 13.51
    new.prev_oos, new.prev_eoh_ow = 9.0, 3_900.0
    out = pos_brief.render_weekly(_with_extra_sku(new))
    assert "publishes a $PSPW goal but not yet a door count" in out["main"]
    assert "Mini Disp w/LM 40ct Peri (~1,053 doors)" in out["main"]
    assert "~1,053" in out["replies"][0]


def test_a_goaled_sku_with_no_doors_stays_out_of_the_goal_tables() -> None:
    """A goal alone is not attainment. A newly-listed item can carry a goal and
    still have no doors at all — no KMG count and no inventory row yet — and the
    Top 5 table prints a $PSPW unconditionally, so it must not reach it."""
    new = pos_brief._annotate([_sku("007-08-5892", amt=99_999.0, units=6_000.0, doors=None)], CFG)[
        0
    ]
    new.prev_amt, new.eoh_ow, new.wip, new.oos = 80_000.0, None, None, None
    out = pos_brief.render_weekly(_with_extra_sku(new))
    assert new.pspw is None
    top5 = out["main"].split("**Top 5 SKUs")[1]
    assert "Mini Disp w/LM 40ct Peri" not in top5
    assert "Mini Disp w/LM 40ct Peri" in out["replies"][0]  # still in the full detail


# ---------- pulse mode ----------


def _pulse_input(skus: list) -> dict:
    """Minimum input render_pulse accepts: a 3-day mid-week span."""
    return {
        "wtd": {
            "days": 3,
            "week_start": date(2026, 8, 16),
            "cur": SimpleNamespace(amt=171_601.0, units=18_271.0),
            "prev": SimpleNamespace(amt=160_716.0, units=17_417.0),
            "avg4_span": 157_499.0,
            "peer_weeks": 4,
        },
        "skus": skus,
        "sales_through": date(2026, 8, 18),
        "inv_through": date(2026, 8, 18),
    }


def _pulse_sku(dpci, amt, oos, prev_oos, eoh, prev_eoh, wip=None):
    s = pos_brief._annotate([_sku(dpci, amt=amt, units=amt / 2.7)], CFG)[0]
    s.prev_amt, s.oos, s.prev_oos = amt * 0.95, oos, prev_oos
    s.eoh_ow, s.prev_eoh_ow, s.wip = eoh, prev_eoh, (100 - oos) if wip is None else wip
    return s


def test_pulse_leads_with_a_breach_and_separates_the_watch_list() -> None:
    """Mid-week is the only time a developing stockout can still be acted on, so
    a SKU past the goal must not render in the same flat format as one at 3%.
    HS Go-Pack went 9.9% -> 29.3% between two Tuesdays while cover fell 5,738 ->
    3,788; as the first line of a four-item list that reads as routine."""
    breach = _pulse_sku("003-02-7872", 5_026.0, 29.3, 9.9, 3_788.0, 5_738.0, wip=70.7)
    minor = _pulse_sku("003-02-5627", 7_016.0, 3.7, 3.1, 8_499.0, 8_800.0)
    main = pos_brief.render_pulse(_pulse_input([breach, minor]))["main"]

    callout = next(ln for ln in main.splitlines() if "past the" in ln)
    assert "Dispenser - Black" in callout
    assert "OOS 29.3%" in callout and "9.9% a week ago" in callout
    assert "5,738 → 3,788 units" in callout
    # The breach leads; the sub-goal SKU is demoted to the watch list, not mixed in.
    assert main.index(callout) < main.index("Also watching")
    watch = main.split("Also watching")[1]
    assert "Disinf Refill 180ct Var" in watch
    assert "Dispenser - Black" not in watch, "a breach must not be repeated below"


def test_pulse_flags_an_implausible_full_oos_as_a_feed_gap_not_a_breach() -> None:
    """HS Go-Pack 20ct Santal read 100.0% OOS on 2026-09-06..09-08 while still
    selling 36-54 units/day in-store — the per-location stock-status flags had
    dropped out for all but 2 of ~319 doors, not the item going empty
    everywhere. A SKU that is still selling cannot also be universally out of
    stock, so it must not render as an actionable breach."""
    gap = _pulse_sku("003-02-7872", 5_026.0, 100.0, 41.7, 1_592.0, 1_818.0)
    gap.units = 113.0
    minor = _pulse_sku("003-02-5627", 7_016.0, 3.7, 3.1, 8_499.0, 8_800.0)
    main = pos_brief.render_pulse(_pulse_input([gap, minor]))["main"]

    assert "past the" not in main
    assert "🟠" in main
    line = next(ln for ln in main.splitlines() if "inventory feed gap" in ln)
    assert "Dispenser - Black" in line
    assert "OOS reads 100.0%" in line and "sold 113 units" in line
    watch = main.split("Also watching")[1]
    assert "Dispenser - Black" not in watch


def test_pulse_reports_a_limited_time_item_as_sell_through_not_a_breach() -> None:
    """The HS 20ct go-pack and the LGR+PUR baby kits are limited-time buys that
    are MEANT to sell out, so a rising OOS on them is the buy working. Raising
    one as a breach puts an expected sell-out next to a genuine one and trains
    the reader to skim past the flags that do need acting on."""
    lto = _pulse_sku("007-07-1897", 6_117.0, 100.0, 41.7, 1_592.0, 1_818.0)
    lto.units = 113.0
    breach = _pulse_sku("003-02-5627", 7_016.0, 8.2, 6.0, 8_499.0, 8_800.0)
    main = pos_brief.render_pulse(_pulse_input([lto, breach]))["main"]

    sell = next(ln for ln in main.splitlines() if "Baby Kit" in ln and "cover left" in ln)
    assert "113 units so far this week" in sell and "1,592 units of cover left" in sell
    # An LTO's in-stock rate is both expected and — once its doors drop out of
    # the stock-status feed — unreliable, so the rate itself is not printed.
    assert "%" not in sell
    # The genuine breach still leads; the LTO reaches no alert bucket at all.
    assert "Disinf Refill 180ct Var is past the" in main
    assert "Baby Kit - Seafoam Grn is past the" not in main
    assert "inventory feed gap" not in main
    assert "Also watching" not in main


def test_pulse_omits_the_breach_section_when_everything_is_in_stock() -> None:
    ok = _pulse_sku("003-02-5627", 7_016.0, 1.2, 1.0, 8_499.0, 8_600.0)
    main = pos_brief.render_pulse(_pulse_input([ok]))["main"]
    assert "past the" not in main
    assert "Also watching" not in main, "1.2% is under the 2% floor"


def test_pulse_states_the_peer_window_it_actually_used() -> None:
    """A silently-zero peer count would drop the pace line entirely; when it is
    non-zero the reader is told how many weeks it averaged."""
    d = _pulse_input([_pulse_sku("003-02-5627", 7_016.0, 1.0, 1.0, 8_499.0, 8_600.0)])
    assert "prior 4 weeks" in pos_brief.render_pulse(d)["main"]
    d["wtd"]["avg4_span"] = None
    assert "prior" not in pos_brief.render_pulse(d)["main"]


def _series_from(amounts: list[float]) -> list:
    """Weeks newest-first from the given amounts, ending Sat 2026-08-22."""
    weeks = [date(2026, 8, 22) - timedelta(days=7 * i) for i in range(len(amounts))]
    return [
        SimpleNamespace(
            week_end=w,
            days=7,
            amt=a,
            units=a / 9.4,
            promo_amt=a * 0.5,
            online_amt=a * 0.35,
            doors=1814,
        )
        for w, a in zip(weeks, amounts, strict=True)
    ]


def test_a_broken_win_streak_is_reported_not_dropped() -> None:
    """The week after "6 straight weekly gains", a bare -1.4% drops the thread —
    a run ending is the most notable thing about the first down week."""
    d = _render_input(394_268.0, record_high=399_915.0)
    d["series"] = _series_from(
        [394_268, 399_915, 391_030, 370_753, 346_047, 341_969, 310_247, 275_062]
    )
    d["inventory"] = {
        r.week_end: SimpleNamespace(eoh_ow=450_000.0, wip=98.9, oos=1.1) for r in d["series"]
    }
    d["pspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 273.78)
    d["upspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 30.8)
    lead = pos_brief.render_weekly(d)["main"].splitlines()[2]
    assert "ends a 6-week run of gains" in lead
    assert "straight weekly gains" not in lead


def test_an_ongoing_streak_still_reads_as_a_streak() -> None:
    d = _render_input(399_915.0, record_high=399_915.0)
    d["series"] = _series_from([399_915, 391_030, 370_753, 346_047, 341_969, 310_247])
    d["inventory"] = {
        r.week_end: SimpleNamespace(eoh_ow=450_000.0, wip=99.1, oos=0.9) for r in d["series"]
    }
    d["pspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 276.97)
    d["upspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 31.4)
    lead = pos_brief.render_weekly(d)["main"].splitlines()[2]
    assert "5 straight weekly gains" in lead
    assert "ends a" not in lead


def test_inventory_line_states_the_sales_change_rather_than_asserting_a_direction() -> None:
    """ "with sales rising" next to a -1.4% WoW headline reads as a contradiction
    even when both are true on their own windows. Print the window's number."""
    d = _render_input(394_268.0, record_high=399_915.0)
    d["series"] = _series_from([394_268, 399_915, 391_030, 370_753])
    d["inventory"] = {
        r.week_end: SimpleNamespace(eoh_ow=446_518.0 + i * 20_000, wip=98.9, oos=1.1)
        for i, r in enumerate(d["series"])
    }
    d["pspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 273.78)
    d["upspw_by_week"] = dict.fromkeys([r.week_end for r in d["series"]], 30.8)
    main = pos_brief.render_weekly(d)["main"]
    line = next(ln for ln in main.splitlines() if "Inventory is unwinding" in ln)
    assert "with sales rising" not in line
    assert "across the same window" in line and "+6.3%" in line
