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
from datetime import date
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
        (date(2026, 8, 1), "Jul W4 '26"),  # starts Sun Jul 26 — 4th Sunday of July
        (date(2026, 8, 8), "Aug W1 '26"),  # starts Sun Aug 2 — 1st Sunday of August
        (date(2026, 7, 4), "Jun W4 '26"),  # starts Sun Jun 28 — 4th Sunday of June
        (date(2026, 1, 3), "Dec W4 '25"),  # start month carries the year across
    ],
)
def test_fiscal_label_names_the_week_by_its_start(week_end: date, label: str) -> None:
    """KMG labels a week by the month it STARTS in; labelling by week-end would
    call w/e Aug 1 "Aug W1" and disagree with every published report."""
    assert pos_brief._fiscal_label(week_end) == label


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
        "007-07-1897": {"name": "Baby Kit - Seafoam Grn", "goal_pspw": None, "pog_doors": 1387},
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


# ---------- the shipped config ----------


def test_shipped_goals_file_is_well_formed() -> None:
    cfg = json.loads((ROOT / "config" / "pspw_goals.json").read_text())
    assert cfg["as_of"] and cfg["source"]
    assert not set(cfg["assortment"]) & set(cfg["excluded"]), "a DPCI cannot be both"
    for dpci, meta in cfg["assortment"].items():
        assert meta["name"], dpci
        assert isinstance(meta["pog_doors"], int) and meta["pog_doors"] > 0, dpci
        assert meta["goal_pspw"] is None or meta["goal_pspw"] > 0, dpci
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
    ungoaled.wip, ungoaled.oos = 93.6, 6.42

    delisted = pos_brief._annotate([_sku("999-99-9999", amt=28.0, units=2.0, doors=3)], CFG)[0]
    delisted.prev_amt, delisted.eoh_ow = 20.0, 44.0
    delisted.wip, delisted.oos = 20.0, 80.0

    d["skus"] = [*d["skus"], ungoaled, delisted]
    flag_line = next(
        ln for ln in pos_brief.render_weekly(d)["main"].splitlines() if "Highest OOS" in ln
    )
    assert "Dispenser - Black 6.4%" in flag_line
    assert "80.0%" not in flag_line


def test_render_reports_the_short_week_it_dropped() -> None:
    """Excluding a week from the averages is a judgement call, so it is stated
    in the footer rather than applied silently."""
    out = pos_brief.render_weekly(_render_input(391_030.0, record_high=391_030.0))
    assert "w/e 2026-05-09 (4/7 days)" in out["main"]
    assert "quantity-weighted" in out["main"]
    assert len(out["replies"]) == 2
