"""DTC performance brief from the bpd-mcp analytics tools → Slack-ready text.

The DTC counterpart of scripts/pos_brief.py, on the cadence the ecomm team asked
for in #ecommerce (weekly, not daily). Three modes, each a `main` message plus
threaded `replies`:

  weekly (Mondays)   the Mon–Sun week that just closed as a three-part
                     scorecard — Revenue & Efficiency, Acquisition, Retention &
                     Subscription Health — every line against the team's goal
                     for those seven days AND the prior week; then the month to
                     date. Replies: the week day by day; the 8-week trend.
  subpulse (Thurs)   the mid-week subscriber pulse: active subscribers, new
                     subscribers and subscriber reductions, each as the level
                     so far with what has moved since Monday. Nothing else —
                     it exists to catch a subscriber problem before the week
                     closes, not to re-report the week.
  recap  (1st)       the month that just closed vs its forecast, last month and
                     last year, with the certified net revenue for the P&L
                     tie-out. Replies: by week; by day.

THE LIGHTS. Every scorecard line carries a dot, on one convention (the ecomm
team's, 2026-09-21):

  🟢  on or above goal AND improving vs the prior period
  🟡  moving unfavorably by less than WATCH_VARIANCE_PCT (15%) against goal or
      the prior period — worth watching, not yet concerning
  🔴  moving unfavorably by more than 15% against either — needs attention
  ⚪  nothing to judge against (no goal loaded and no prior period)

A line is coloured by its WORST available comparison, so "green" really does
mean both are fine. Direction is per metric: CAC, subscriber reductions and ad
spend read the other way (higher is unfavourable), and net subscriber growth is
red whenever it is negative, however small the move. Where the pacing sheet
states no goal — every subscriber metric except recurring revenue — the line is
judged on the prior period alone; that is the convention, not a gap.

WHERE THE NUMBERS COME FROM. Every actual is BigQuery, through the server's own
tools (tools/dtc.py): `bpd_get_dtc_pacing` for the month, `bpd_get_marketing_
efficiency` for weeks and days, `bpd_get_subscription_health` for subscribers.
The brief can therefore never disagree with what someone gets from the MCP by
hand. The ecomm team's pacing sheet contributes targets and nothing else —
config/dtc_pacing_targets.json, regenerated monthly (see pacing_targets.py).
"Total revenue" is the sheet's plan basis, which it calls demand: net sales
after discounts plus shipping (Shopify total less tax) on paid core-D2C orders,
one row per order.

Usage:
    uv run python scripts/dtc_brief.py --mode weekly
    uv run python scripts/dtc_brief.py --mode subpulse
    uv run python scripts/dtc_brief.py --mode recap
    uv run python scripts/dtc_brief.py --mode weekly --as-of 2026-09-07 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bpd_mcp.pacing_targets import PacingTargets, TargetsUnavailable, load_targets
from bpd_mcp.schemas import (
    DtcPacingInput,
    MarketingEfficiencyInput,
    SubscriptionHealthInput,
)
from bpd_mcp.tools import dtc

#: The yellow band. A metric moving unfavorably by less than this against goal
#: or the prior period is 🟡; beyond it, 🔴. The ecomm team set it at 15% on
#: 2026-09-21 (it was 10% while the brief scored demand and spend alone).
WATCH_VARIANCE_PCT = 15.0

TREND_WEEKS = 8

GREEN, YELLOW, RED, NEUTRAL = "🟢", "🟡", "🔴", "⚪"

#: Plan key -> the sheet's daily forecast series it is summed from. NC ROAS and
#: CAC are derived from these as ratios of the sums. `recurring_revenue` is the
#: sheet's "Projected Sub Rev", which paces the RECURRING half of subscription
#: revenue (see pacing_targets.FIELD_MAP for the reconciliation).
PLAN_SERIES = {
    "demand": "forecast_demand",
    "spend": "forecast_spend",
    "new_customers": "forecast_new_customers",
    "nc_demand": "forecast_nc_demand",
    "recurring_revenue": "forecast_recurring_revenue",
}

#: Metrics with no goal anywhere in the pacing sheet. Listed so the "no plan for
#: this week" note can say the subscriber lines are prior-period-only BY DESIGN
#: rather than leaving a reader to wonder which absence they are looking at.
GOALLESS_METRICS = (
    "subscriber take rate",
    "active subscribers",
    "subscriber additions",
    "subscriber reductions",
    "net subscriber growth",
    "active MRR",
)


# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------


def _k(v: Any) -> str:
    """$ in thousands to one decimal for headline figures."""
    if v is None:
        return "n/a"
    return f"${v / 1000:,.1f}K" if abs(v) >= 10_000 else f"${v:,.0f}"


# $ and % render exactly as the tools' own markdown does (n/a for None), so a
# figure in the brief and the same figure from the MCP read identically.
_money = dtc._money
_pct_change = dtc._pct_change


def _pct(v: Any) -> str:
    """The tools' percentage formatter, with a hair below zero shown as zero.

    A rate that is 0.03% off its goal prints "1.60x vs 1.60x goal (-0.0%)",
    which reads as a miss of something too small to see. The line is on goal;
    say so."""
    if v is not None and abs(v) < 0.05:
        v = 0.0
    return dtc._pct(v)


def _chg(actual: Any, base: Any) -> float | None:
    """Percent change FOR A LIGHT, including the zero base `_pct_change` cannot
    express (it returns None, which the lights read as "nothing to judge").

    A week with 4 subscriber additions after a week with none is not
    unknowable, it is growth; a week with none after a week with four is the
    opposite; none after none is flat. Judging those ⚪ hid exactly the weeks
    worth looking at. The printed line still shows "prior week 0" rather than a
    made-up +100%, because the magnitude is what is unknowable, not the sign.
    """
    if actual is None or base is None:
        return None
    if base == 0:
        return 0.0 if actual == 0 else (100.0 if actual > 0 else -100.0)
    return _pct_change(actual, base)


def _x(v: Any) -> str:
    return "n/a" if v is None else f"{v:.2f}x"


def _n(v: Any) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def _signed(v: Any) -> str:
    return "n/a" if v is None else f"{v:+,.0f}"


def _share(v: Any) -> str:
    """A 0-1 share as an unsigned percentage — a LEVEL, not a change."""
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _light(
    *variances: float | None,
    good_when_high: bool = True,
    force_red: bool = False,
) -> str:
    """The dot for a line, from every variance it can be judged on.

    Each argument is a percent change of the actual against one base (the goal,
    the prior period). None means "no such comparison" and is skipped. The line
    takes its colour from the WORST of the ones it has, so 🟢 means every
    available comparison is favourable — on/above goal AND improving — not
    merely that the first one was.

    `good_when_high=False` flips the sense for cost-type metrics (CAC, spend,
    subscriber reductions), where a rise is the unfavourable direction.
    `force_red` is the net-growth rule: negative net growth is red whatever the
    percentages say.
    """
    if force_red:
        return RED
    favorable = [(v if good_when_high else -v) for v in variances if v is not None]
    if not favorable:
        return NEUTRAL
    worst = min(favorable)
    if worst >= 0:
        return GREEN
    if worst >= -WATCH_VARIANCE_PCT:
        return YELLOW
    return RED


def _rate_light(
    actual: Any,
    goal: Any,
    prior: Any,
    fmt: Any,
    *,
    good_when_high: bool = True,
) -> str:
    """A rate's dot, judged at the precision it is PRINTED at: if the actual and
    the goal render identically ("1.60x vs 1.60x goal") the line is on goal,
    never yellow over a third decimal the reader cannot see."""
    goal_var = _chg(actual, goal)
    if actual is not None and goal is not None and fmt(actual) == fmt(goal):
        goal_var = 0.0
    prior_var = _chg(actual, prior)
    if actual is not None and prior is not None and fmt(actual) == fmt(prior):
        prior_var = 0.0
    return _light(goal_var, prior_var, good_when_high=good_when_high)


def _table(rows: list[list[str]], aligns: str) -> str:
    """Fixed-width text table for a Slack code block (same shape as pos_brief's)."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [
            r[i].ljust(widths[i]) if aligns[i] == "l" else r[i].rjust(widths[i])
            for i in range(len(r))
        ]
        out.append(" ".join(cells).rstrip())
    return "\n".join(out)


def _span(a: date, b: date) -> str:
    return f"{a:%a %b %-d}–{b:%a %b %-d}" if a != b else f"{a:%a %b %-d}"


def _stat(light: str, label: str, text: str) -> str:
    return f"{light} **{label}**  {text}"


def _vs_goal(actual: float | None, goal: float | None, fmt: Any, *, pct: bool = True) -> str:
    """`$63.4K vs $66.9K goal (-5.2%)`, or just the actual when there is no goal."""
    if goal is None:
        return str(fmt(actual))
    s = f"{fmt(actual)} vs {fmt(goal)} goal"
    if pct:
        s += f" ({_pct(_pct_change(actual, goal))})"
    return s


def _vs_prior(
    actual: float | None,
    prior: float | None,
    fmt: Any,
    *,
    label: str = "prior week",
    as_level: bool = False,
) -> str:
    """The prior-period half of a line: a % change for volumes, the earlier
    LEVEL for rates and for signed counts a percentage would make unreadable."""
    if prior is None:
        return ""
    if as_level or _pct_change(actual, prior) is None:
        # A zero base has no percentage: show the level it moved from instead
        # of "n/a vs prior week", which reads as a tool failure.
        return f" · {label} {fmt(prior)}"
    return f" · {_pct(_pct_change(actual, prior))} vs {label}"


# --------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------


def plan_for(targets: PacingTargets | None, start: date, end: date) -> dict[str, float | None]:
    """The team's goal for a span of days: the daily forecasts summed, plus the
    implied MER, NC ROAS and CAC as ratios of those sums (how the sheet's own BRoAS
    and NC RoAS columns are built). A series is None unless EVERY day in the span
    carries it — a week straddling a month whose tab has not landed has no goal,
    not half of one. NC ROAS falls back to the mean of the sheet's daily NC RoAS
    column when a tab carries that but not NC DMD, mirroring the pacing tool."""
    out: dict[str, float | None] = dict.fromkeys(PLAN_SERIES)
    out.update(mer=None, nc_roas=None, cac=None)
    if targets is None:
        return out
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def series(fld: str) -> list[float] | None:
        vals: list[float] = []
        for d in days:
            v = _plan_day(targets, d, fld)
            if v is None:
                return None
            vals.append(v)
        return vals

    for key, fld in PLAN_SERIES.items():
        vals = series(fld)
        out[key] = sum(vals) if vals else None
    out["mer"] = dtc._ratio(out["demand"], out["spend"])
    out["nc_roas"] = dtc._ratio(out["nc_demand"], out["spend"])
    if out["nc_roas"] is None:
        rates = series("forecast_nc_roas")
        out["nc_roas"] = sum(rates) / len(rates) if rates else None
    out["cac"] = dtc._ratio(out["spend"], out["new_customers"])
    return out


def _plan_day(targets: PacingTargets | None, d: date, fld: str) -> float | None:
    if targets is None:
        return None
    m = targets.month(d.strftime("%Y-%m"))
    return m.days[d].get(fld) if m is not None and d in m.days else None


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


def last_complete_week(today: date) -> tuple[date, date]:
    """The most recent Monday–Sunday week whose Sunday is before today."""
    end = today - timedelta(days=1)
    end -= timedelta(days=(end.weekday() + 1) % 7)  # back to a Sunday
    return end - timedelta(days=6), end


def _ok(resp: Any, what: str) -> dict[str, Any]:
    if not resp.ok:
        raise RuntimeError(f"{what}: {resp.error.code}: {resp.error.message}")
    data: dict[str, Any] = resp.data
    return data


async def _days(wh: Any, start: date, end: date, what: str) -> dict[date, dict[str, Any]]:
    """Efficiency rows at day grain, keyed by date."""
    data = _ok(
        await dtc.get_marketing_efficiency(
            wh,
            MarketingEfficiencyInput(
                grain="day", start_date=start, end_date=end, response_format="json"
            ),
        ),
        what,
    )
    out: dict[date, dict[str, Any]] = {}
    for r in data["rows"]:
        d = r["period"] if isinstance(r["period"], date) else date.fromisoformat(str(r["period"]))
        out[d] = r
    return out


async def _subs(wh: Any, start: date, end: date, what: str) -> dict[str, Any]:
    """Subscriber health for one window (see tools/dtc.get_subscription_health)."""
    return _ok(
        await dtc.get_subscription_health(
            wh,
            SubscriptionHealthInput(start_date=start, end_date=end, response_format="json"),
        ),
        what,
    )


async def gather(
    mode: str,
    as_of: date | None = None,
    *,
    warehouse: Any = None,
    targets: PacingTargets | None = None,
) -> dict[str, Any]:
    """Run the tool calls one brief needs and return their JSON payloads, untouched.

    `warehouse` is an injection point for the tests (a fixture warehouse of literal
    rows); the CLI leaves it None and the server's own context is built and closed
    here. `targets` defaults to the checked-in config; the tests pass a synthetic one.
    """
    app = None
    if warehouse is None:
        from bpd_mcp.server import build_context

        app = await build_context()
        warehouse = app.warehouse
    wh = warehouse
    if targets is None:
        try:
            targets = load_targets()
        except TargetsUnavailable:
            targets = None  # the pacing tool reports the same absence in `targets`
    today = as_of or dtc.today_reporting()
    yesterday = today - timedelta(days=1)
    try:
        if mode == "recap":
            prev_month_last = today.replace(day=1) - timedelta(days=1)
            ym = prev_month_last.strftime("%Y-%m")
            pacing = _ok(
                await dtc.get_dtc_pacing(
                    wh,
                    DtcPacingInput(as_of=yesterday, month=ym, response_format="json"),
                    targets=targets,
                ),
                "pacing",
            )
            m_start, m_end = dtc.month_bounds(ym)
            month = _ok(
                await dtc.get_marketing_efficiency(
                    wh,
                    MarketingEfficiencyInput(
                        grain="month", start_date=m_start, end_date=m_end, response_format="json"
                    ),
                ),
                "efficiency (month)",
            )
            return {"mode": mode, "today": today, "pacing": pacing, "month": month}

        if mode == "subpulse":
            # The mid-week subscriber pulse asks two questions of the same tool:
            # where the book stands month to date, and what has moved since the
            # week opened. Monday is the week's own Monday even when the brief
            # runs later; the window ends on the last COMPLETE day.
            wk_start = yesterday - timedelta(days=yesterday.weekday())
            m_start = yesterday.replace(day=1)
            return {
                "mode": mode,
                "today": today,
                "wtd": (wk_start, yesterday),
                "mtd": (m_start, yesterday),
                "since_monday": await _subs(wh, wk_start, yesterday, "subscribers (this week)"),
                "month_subs": await _subs(wh, m_start, yesterday, "subscribers (month to date)"),
            }

        pacing = _ok(
            # as_of is the last COMPLETE day, so a --as-of backtest paces through the
            # day before the simulated today, exactly as a live run does.
            await dtc.get_dtc_pacing(
                wh, DtcPacingInput(as_of=yesterday, response_format="json"), targets=targets
            ),
            "pacing",
        )
        wk_start, wk_end = last_complete_week(today)
        prev_start, prev_end = wk_start - timedelta(days=7), wk_end - timedelta(days=7)
        weeks = _ok(
            await dtc.get_marketing_efficiency(
                wh,
                MarketingEfficiencyInput(
                    grain="week",
                    start_date=wk_start - timedelta(weeks=TREND_WEEKS - 1),
                    end_date=wk_end,
                    response_format="json",
                ),
            ),
            "efficiency (weeks)",
        )
        shift = timedelta(days=dtc.LY_SHIFT_DAYS)
        return {
            "mode": mode,
            "today": today,
            "week": (wk_start, wk_end),
            "pacing": pacing,
            "weeks": weeks,
            "days": await _days(wh, wk_start, wk_end, "efficiency (days)"),
            "ly_days": await _days(wh, wk_start - shift, wk_end - shift, "efficiency (LY)"),
            "plan": plan_for(targets, wk_start, wk_end),
            "subs": await _subs(wh, wk_start, wk_end, "subscribers (week)"),
            "prev_subs": await _subs(wh, prev_start, prev_end, "subscribers (prior week)"),
            "targets": targets,
        }
    finally:
        if app is not None:
            await app.aclose()


# --------------------------------------------------------------------------------------
# Shared blocks
# --------------------------------------------------------------------------------------


def _sum_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll efficiency rows (any grain) into one window with recomputed ratios."""
    tot: dict[str, Any] = dict.fromkeys(
        (
            "spend",
            "orders",
            "new_customers",
            "demand",
            "nc_orders",
            "nc_demand",
            "gross_sales",
            "admin_net_revenue",
        ),
        0.0,
    )
    for r in rows:
        for k in tot:
            tot[k] += float(r.get(k) or 0)
    tot["mer"] = tot["demand"] / tot["spend"] if tot["spend"] else None
    tot["nc_roas"] = tot["nc_demand"] / tot["spend"] if tot["spend"] else None
    tot["nc_aov"] = tot["nc_demand"] / tot["nc_orders"] if tot["nc_orders"] else None
    tot["cac"] = tot["spend"] / tot["new_customers"] if tot["new_customers"] else None
    tot["aov"] = tot["demand"] / tot["orders"] if tot["orders"] else None
    tot["nc_share"] = tot["new_customers"] / tot["orders"] if tot["orders"] else None
    return tot


def revenue_block(
    cur: dict[str, Any],
    goal: dict[str, Any],
    prev: dict[str, Any] | None = None,
    *,
    prior_label: str = "prior week",
    revenue_extra: str = "",
    spend_extra: str = "",
) -> list[str]:
    """**Revenue & Efficiency** — total revenue, new-customer ROAS, ad spend.

    "Total revenue" is the pacing sheet's own plan basis (it calls the column
    Forecast and the actual Actual DMD): net sales after discounts plus shipping
    on paid core-D2C orders. New-customer ROAS is NC demand / spend — the
    sheet's "NC RoAS", a.k.a. aMER — the ratio acquisition spend actually
    controls. Ad spend is judged as a cost: over goal is the unfavourable
    direction, and an underspend gets a word rather than a red dot, because
    spending under plan is a decision the team makes, not a miss.
    """
    p = prev or {}
    spend_var = _pct_change(cur.get("spend"), goal.get("spend"))
    spend_note = ""
    if spend_var is not None and spend_var >= WATCH_VARIANCE_PCT:
        spend_note = " · over goal — check efficiency"
    elif spend_var is not None and spend_var <= -WATCH_VARIANCE_PCT:
        spend_note = spend_extra or " · under goal"
    return [
        _stat(
            _light(
                _chg(cur.get("demand"), goal.get("demand")),
                _chg(cur.get("demand"), p.get("demand")),
            ),
            "Total revenue",
            _vs_goal(cur.get("demand"), goal.get("demand"), _k)
            + _vs_prior(cur.get("demand"), p.get("demand"), _k, label=prior_label)
            + revenue_extra,
        ),
        _stat(
            _rate_light(cur.get("nc_roas"), goal.get("nc_roas"), p.get("nc_roas"), _x),
            "New customer ROAS",
            _vs_goal(cur.get("nc_roas"), goal.get("nc_roas"), _x)
            + _vs_prior(
                cur.get("nc_roas"), p.get("nc_roas"), _x, label=prior_label, as_level=True
            )
            + (f" · NC AOV {_money(cur['nc_aov'])}" if cur.get("nc_aov") is not None else ""),
        ),
        _stat(
            # Spend is scored against the GOAL only, as a cost: over goal is the
            # unfavourable direction, under goal is money the team chose not to
            # spend. The prior-period move rides along as context and never
            # colours the line — spending more than last week is not a miss when
            # the week is still inside plan, and treating it as one made every
            # scaling week yellow.
            _light(_chg(cur.get("spend"), goal.get("spend")), good_when_high=False),
            "Ad spend",
            _vs_goal(cur.get("spend"), goal.get("spend"), _k)
            + _vs_prior(cur.get("spend"), p.get("spend"), _k, label=prior_label)
            + spend_note,
        ),
    ]


def acquisition_block(
    cur: dict[str, Any],
    goal: dict[str, Any],
    prev: dict[str, Any] | None = None,
    *,
    take_rate: float | None = None,
    prev_take_rate: float | None = None,
    prior_label: str = "prior week",
) -> list[str]:
    """**Acquisition** — CAC, subscriber take rate, NC demand, new customers.

    Take rate is the share of the window's new customers whose acquiring order
    carried a subscription (tools/dtc.get_subscription_health). The sheet states
    no goal for it, so it is judged on the prior period alone.
    """
    p = prev or {}
    lines = [
        _stat(
            _rate_light(
                cur.get("cac"), goal.get("cac"), p.get("cac"), _money, good_when_high=False
            ),
            "New customer CAC",
            _vs_goal(cur.get("cac"), goal.get("cac"), _money)
            + _vs_prior(cur.get("cac"), p.get("cac"), _money, label=prior_label, as_level=True),
        ),
        _stat(
            _light(_chg(take_rate, prev_take_rate)),
            "Subscriber take rate",
            _share(take_rate)
            + _vs_prior(take_rate, prev_take_rate, _share, label=prior_label, as_level=True),
        ),
        _stat(
            _light(
                _chg(cur.get("nc_demand"), goal.get("nc_demand")),
                _chg(cur.get("nc_demand"), p.get("nc_demand")),
            ),
            "NC demand",
            _vs_goal(cur.get("nc_demand"), goal.get("nc_demand"), _k)
            + _vs_prior(cur.get("nc_demand"), p.get("nc_demand"), _k, label=prior_label),
        ),
        _stat(
            _light(
                _chg(cur.get("new_customers"), goal.get("new_customers")),
                _chg(cur.get("new_customers"), p.get("new_customers")),
            ),
            "New customers",
            _vs_goal(cur.get("new_customers"), goal.get("new_customers"), _n)
            + _vs_prior(cur.get("new_customers"), p.get("new_customers"), _n, label=prior_label),
        ),
    ]
    return lines


def retention_block(
    subs: dict[str, Any],
    prev_subs: dict[str, Any] | None,
    goal: dict[str, Any],
    *,
    prior_label: str = "prior week",
) -> list[str]:
    """**Retention & Subscription Health** — the subscriber book and what it bills.

    Six lines off one `bpd_get_subscription_health` payload per window, so
    additions minus reductions is always the net growth shown and always the
    move in active subscribers. The pacing sheet states one goal here — the
    recurring half of subscription revenue ("Projected Sub Rev") — so the other
    five lines are judged on the prior period alone.

    Two lines break the default direction. Subscriber reductions read as a cost:
    more of them is worse. Net subscriber growth is red whenever it is negative,
    whatever the percentages say — a shrinking book is not a watch item.
    """
    s, r = subs["subscribers"], subs["revenue"]
    ps = (prev_subs or {}).get("subscribers") or {}
    pr = (prev_subs or {}).get("revenue") or {}
    if not subs.get("point_in_time_available"):
        # No point-in-time history for this window: say so once instead of six
        # ⚪ lines that look like a tool failure.
        return [f"⚪ **Subscribers**  n/a — {subs.get('note')}"]

    active, prior_active = s.get("active"), ps.get("active")
    net, prior_net = s.get("net_growth"), ps.get("net_growth")
    recurring_goal = goal.get("recurring_revenue")
    return [
        _stat(
            _light(_chg(active, prior_active)),
            "Active subscribers",
            _n(active) + _vs_prior(active, prior_active, _n, label=prior_label),
        ),
        _stat(
            _light(_chg(s.get("additions"), ps.get("additions"))),
            "Subscriber additions",
            _n(s.get("additions"))
            + _vs_prior(s.get("additions"), ps.get("additions"), _n, label=prior_label),
        ),
        _stat(
            _light(
                _chg(s.get("reductions"), ps.get("reductions")), good_when_high=False
            ),
            "Subscriber reductions",
            _n(s.get("reductions"))
            + _vs_prior(s.get("reductions"), ps.get("reductions"), _n, label=prior_label),
        ),
        _stat(
            _light(
                _chg(net, prior_net),
                force_red=net is not None and net < 0,
            ),
            "Net subscriber growth",
            _signed(net)
            + _vs_prior(net, prior_net, _signed, label=prior_label, as_level=True),
        ),
        _stat(
            _light(
                _chg(r.get("recurring_revenue"), recurring_goal),
                _chg(r.get("subscription_revenue"), pr.get("subscription_revenue")),
            ),
            "Subscription revenue",
            f"{_k(r.get('subscription_revenue'))} — checkout {_k(r.get('checkout_revenue'))} · "
            f"recurring {_vs_goal(r.get('recurring_revenue'), recurring_goal, _k)}"
            + _vs_prior(
                r.get("subscription_revenue"),
                pr.get("subscription_revenue"),
                _k,
                label=prior_label,
            ),
        ),
        _stat(
            _light(_chg(s.get("active_mrr"), ps.get("active_mrr"))),
            "Active MRR",
            _k(s.get("active_mrr"))
            + _vs_prior(s.get("active_mrr"), ps.get("active_mrr"), _k, label=prior_label),
        ),
    ]


def month_block(p: dict[str, Any]) -> list[str]:
    """The month to date, scored against the sheet's goal; `final` shape once closed.

    The same vocabulary as the week's buckets (total revenue, new-customer ROAS,
    CAC, ad spend) so one number never has two names in one message. It carries
    no subscriber lines: the sheet has no month goal for them, and the week's
    Retention block already answers the question.
    """
    s = p["summary"]
    mtd, fc = s["mtd"], s.get("forecast_mtd") or {}
    fm, tg, rd = (
        s.get("month_forecast") or {},
        s.get("to_go") or {},
        s.get("required_daily_average") or {},
    )
    rr, pvf = s["run_rate_projection"], s.get("projection_vs_forecast") or {}
    ly, pm = s["last_year_mtd"], s["prior_month_to_date"]
    through = date.fromisoformat(s["complete_through"])
    done = s["days_left"] == 0
    head = (
        f"**{through:%B} — final** ({s['elapsed_days']} days)"
        if done
        else f"**{through:%B} — day {s['elapsed_days']} of {s['days_in_month']}**"
    )
    revenue_extra = ""
    if not done and fm.get("demand") is not None:
        revenue_extra = (
            f" · run-rate {_k(rr['demand'])} vs {_k(fm['demand'])} forecast "
            f"({_pct(pvf.get('demand_pct'))})"
        )
    goal = {
        "demand": fc.get("demand"),
        "spend": fc.get("spend"),
        "new_customers": fc.get("new_customers"),
        "nc_demand": fc.get("nc_demand"),
        "mer": fc.get("mer"),
        "nc_roas": fc.get("nc_roas"),
        "cac": fc.get("cac"),
    }
    lines = [
        head,
        *revenue_block(
            mtd,
            goal,
            revenue_extra=revenue_extra,
            spend_extra=" · room to scale if efficiency holds",
        ),
        *acquisition_block(mtd, goal)[:1],  # CAC; take rate and NC lines follow
        _stat(
            _light(_chg(mtd.get("nc_demand"), goal.get("nc_demand"))),
            "NC demand",
            _vs_goal(mtd.get("nc_demand"), goal.get("nc_demand"), _k),
        ),
        _stat(
            _light(_chg(mtd.get("new_customers"), goal.get("new_customers"))),
            "New customers",
            _vs_goal(mtd.get("new_customers"), goal.get("new_customers"), _n),
        ),
    ]
    tail = []
    if not done and tg.get("demand") is not None and rd.get("demand") is not None:
        if tg["demand"] > 0:
            tail.append(
                f"To go {_k(tg['demand'])} over {s['days_left']} days ({_k(rd['demand'])}/day)"
            )
        else:
            tail.append(
                f"Already {_k(-tg['demand'])} past the month forecast with {s['days_left']} days left"
            )
    tail.append(f"vs LY {_pct(ly['demand_change_pct'])}")
    tail.append(f"vs same days last month {_pct(pm['demand_change_pct'])}")
    lines.append(" · ".join(tail))
    return lines


def notes_block(p: dict[str, Any]) -> list[str]:
    """Only what the lights cannot show: missing targets and the LY caveat."""
    s = p["summary"]
    notes: list[str] = []
    if s.get("last_year_note"):
        notes.append(s["last_year_note"] + ".")
    t = p.get("targets") or {}
    if t.get("status") != "ok":
        notes.append(
            f"No goal loaded for this month ({t.get('status')}); the month lines show actuals "
            f"only. {t.get('note') or ''}".strip()
        )
    return notes


def subscriber_notes(subs: dict[str, Any] | None) -> list[str]:
    """What the subscriber lines are judged against, and what they cannot say."""
    if not subs:
        return []
    notes = [
        "The pacing sheet sets no goal for the subscriber lines or the take rate ("
        + ", ".join(GOALLESS_METRICS)
        + "), so those are scored against the prior period alone."
    ]
    if subs.get("note"):
        notes.append(str(subs["note"]) + ".")
    return notes


def week_plan_note(plan: dict[str, Any], start: date, end: date) -> list[str]:
    """Why the week lines are unscored, when they are: a day in the span has no
    forecast (its month's tab is not in the config), so there is no week goal."""
    if plan.get("demand") is not None:
        return []
    return [
        f"No goal for {_span(start, end)}: the pacing sheet has no forecast for every day in "
        f"it (a month's tab is missing from the config), so those lines show actuals only."
    ]


def footer(p: dict[str, Any], through: date, *, with_subscribers: bool = False) -> str:
    """Provenance line. `through` is the brief's own window end; when the month
    block runs further (an off-schedule run mid-week) both dates are shown."""
    t = p.get("targets") or {}
    src = t.get("source") or {}
    paced = date.fromisoformat(p["summary"]["complete_through"])
    bits = [
        f"data through {through:%a %b %-d}"
        + (f" (month through {paced:%a %b %-d})" if paced != through else "")
    ]
    if t.get("status") == "ok":
        bits.append(
            f'goals: "{t.get("month_tab")}" tab of the pacing sheet, refreshed '
            f"{str(src.get('refreshed_at', '?'))[:10]}"
        )
    bits.append(
        "total revenue = net sales + shipping (Shopify total less tax) on paid core-D2C orders "
        "(the sheet's demand); NC ROAS = new-customer demand / spend; CAC = spend / new "
        "customers; spend = Meta + Google"
    )
    if with_subscribers:
        bits.append(
            "a subscriber is a customer with an ACTIVE Loop contract, counted point-in-time; "
            "additions and reductions are the two differences of the week's opening and "
            "closing subscriber sets, so they always net to the change in active; active MRR "
            "is the book's monthly-normalised billing value, not cash"
        )
    return "_" + " · ".join(bits) + "_"


def daily_table(
    start: date,
    end: date,
    days: dict[date, dict[str, Any]],
    ly_days: dict[date, dict[str, Any]] | None,
    targets: PacingTargets | None,
    *,
    title: str,
) -> str:
    """Day-by-day rows for a span, with the goal and LY per day and a total row."""
    shift = timedelta(days=dtc.LY_SHIFT_DAYS)
    tbl = [["Day", "Revenue", "vs goal", "vs LY", "Spend", "NCs", "CAC"]]
    d = start
    while d <= end:
        r = days.get(d, {})
        ly = (ly_days or {}).get(d - shift, {})
        dem = float(r.get("demand") or 0)
        tbl.append(
            [
                f"{d:%a %-d}",
                _money(dem),
                _pct(_pct_change(dem, _plan_day(targets, d, "forecast_demand"))),
                _pct(_pct_change(dem, float(ly.get("demand") or 0) or None)) if ly_days else "",
                _money(r.get("spend") or 0),
                _n(r.get("new_customers") or 0),
                _money(r.get("blended_cac")),
            ]
        )
        d += timedelta(days=1)
    tot = _sum_rows([days[k] for k in days if start <= k <= end])
    plan = plan_for(targets, start, end)
    ly_tot = (
        _sum_rows([ly_days[k] for k in ly_days if start - shift <= k <= end - shift])
        if ly_days
        else None
    )
    tbl.append(
        [
            "Total",
            _money(tot["demand"]),
            _pct(_pct_change(tot["demand"], plan["demand"])),
            _pct(_pct_change(tot["demand"], ly_tot["demand"] or None)) if ly_tot else "",
            _money(tot["spend"]),
            _n(tot["new_customers"]),
            _money(tot["cac"]),
        ]
    )
    if not ly_days:
        tbl = [row[:3] + row[4:] for row in tbl]
    return f"**{title}**\n```\n{_table(tbl, 'lrrrrrr'[: len(tbl[0])])}\n```"


# --------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------


def render_weekly(d: dict[str, Any]) -> dict[str, Any]:
    wk_start, wk_end = d["week"]
    rows = sorted(d["weeks"]["rows"], key=lambda r: str(r["period"]))
    by = {str(r["period"]): r for r in rows}
    cur_row = by.get(str(wk_start))
    if cur_row is None:
        raise RuntimeError(f"no efficiency row for the week of {wk_start}")
    cur = _sum_rows([cur_row])
    prev_row = by.get(str(wk_start - timedelta(days=7)))
    prev = _sum_rows([prev_row]) if prev_row else None
    trailing = [by[k] for k in sorted(by) if k < str(wk_start)][-4:]
    avg4 = _sum_rows(trailing)["demand"] / len(trailing) if trailing else None
    revenue_extra = (
        f" · {_pct(_pct_change(cur['demand'], avg4))} vs {len(trailing)}-wk avg" if avg4 else ""
    )
    subs, prev_subs = d.get("subs") or {}, d.get("prev_subs") or {}
    take = (subs.get("acquisition") or {}).get("take_rate")
    prev_take = (prev_subs.get("acquisition") or {}).get("take_rate")

    lines = [f"🛒 **DTC — week of {_span(wk_start, wk_end)}**", "", "**Revenue & Efficiency**"]
    lines += revenue_block(cur, d["plan"], prev, revenue_extra=revenue_extra)
    lines += ["", "**Acquisition**"]
    lines += acquisition_block(
        cur, d["plan"], prev, take_rate=take, prev_take_rate=prev_take
    )
    lines += ["", "**Retention & Subscription Health**"]
    lines += retention_block(subs, prev_subs, d["plan"])
    share = f"{cur['nc_share'] * 100:.0f}%" if cur["nc_share"] is not None else "n/a"
    lines += [
        "",
        f"Blended MER {_x(cur['mer'])} · platform ROAS {_x(cur_row.get('platform_roas'))} · "
        f"AOV {_money(cur['aov'])} · {share} of orders were first orders",
    ]
    lines += ["", *month_block(d["pacing"])]
    notes = (
        week_plan_note(d["plan"], wk_start, wk_end)
        + notes_block(d["pacing"])
        + subscriber_notes(subs)
    )
    if notes:
        lines += ["", "**Notes**", *[f"• {n}" for n in notes]]
    lines += ["", footer(d["pacing"], wk_end, with_subscribers=True)]

    replies = [
        daily_table(
            wk_start,
            wk_end,
            d["days"],
            d.get("ly_days"),
            d.get("targets"),
            title=f"Day by day — {_span(wk_start, wk_end)}",
        )
    ]
    tbl = [["Week", "Revenue", "WoW", "Spend", "NC ROAS", "MER", "NCs", "CAC", "NC share"]]
    p_row = None
    for r in rows[-TREND_WEEKS:]:
        p_start = date.fromisoformat(str(r["period"]))
        tbl.append(
            [
                f"{p_start:%b %-d}",
                _money(r["demand"]),
                _pct(_pct_change(r["demand"], p_row["demand"])) if p_row else "",
                _money(r["spend"]),
                _x(r.get("nc_roas")),
                _x(r["mer_demand"]),
                _n(r["new_customers"]),
                _money(r["blended_cac"]),
                f"{r['new_customer_share'] * 100:.0f}%"
                if r.get("new_customer_share") is not None
                else "n/a",
            ]
        )
        p_row = r
    replies.append(
        f"**{len(rows[-TREND_WEEKS:])}-week trend** (Monday-anchored weeks)\n"
        f"```\n{_table(tbl, 'lrrrrrrrr')}\n```"
    )
    return {"main": "\n".join(lines), "replies": replies}


def render_subpulse(d: dict[str, Any]) -> dict[str, Any]:
    """The mid-week subscriber pulse: three lines, each a level month to date
    with what has moved since Monday beside it.

    No dots. The weekly brief is the scorecard; this exists so a subscriber
    problem — a cancellation spike, a stalled book — is visible on Thursday
    instead of the following Monday, and a colour on three days of data would
    claim more than the numbers support.
    """
    wk_start, through = d["wtd"]
    m_start, _ = d["mtd"]
    wk, mtd = d["since_monday"]["subscribers"], d["month_subs"]["subscribers"]
    if not d["since_monday"].get("point_in_time_available"):
        body = f"⚪ Subscriber counts unavailable — {d['since_monday'].get('note')}"
        return {"main": f"🔁 **DTC — mid-week subscriber pulse**\n\n{body}", "replies": []}

    n = (through - wk_start).days + 1
    lines = [
        f"🔁 **DTC — mid-week subscriber pulse** ({_span(wk_start, through)})",
        "",
        f"**Active subscribers**  {_n(mtd.get('active'))} "
        f"({_signed(wk.get('net_growth'))} since Monday)",
        f"**New subscribers**  {_n(mtd.get('additions'))} so far this month "
        f"({_signed(wk.get('additions'))} since Monday)",
        f"**Subscriber reductions**  {_n(mtd.get('reductions'))} so far this month "
        f"({_signed(wk.get('reductions'))} since Monday)",
    ]
    if n <= 1:
        lines += ["", "ⓘ Only one day of the week has closed — read this as directional."]
    lines += [
        "",
        f"_month to date {m_start:%b %-d}–{through:%b %-d}; since Monday is "
        f"{_span(wk_start, through)} · a subscriber is a customer with an ACTIVE Loop "
        f"contract, counted point-in-time from BigQuery (bpd_get_subscription_health); "
        f"additions and reductions always net to the change in active subscribers_",
    ]
    return {"main": "\n".join(lines), "replies": []}


def render_recap(d: dict[str, Any]) -> dict[str, Any]:
    p = d["pacing"]
    s = p["summary"]
    mtd, fm, fc = s["mtd"], s.get("month_forecast") or {}, s.get("forecast_mtd") or {}
    ly, lym, pm = s["last_year_mtd"], s["last_year_month"], s["prior_month_to_date"]
    month_end = date.fromisoformat(s["complete_through"])
    label = month_end.strftime("%B %Y")
    beat = _pct_change(mtd["demand"], fm.get("demand"))
    verdict = ""
    if beat is not None:
        verdict = (
            " — **beat**" if beat >= 0 else (" — **miss**" if beat < -WATCH_VARIANCE_PCT else "")
        )
    lines = [f"🛒 **DTC — {label} recap**{verdict}", "", "**Month vs forecast**"]
    # Every "vs forecast" is against the sheet's stated month Total — the same
    # base for every line, not the MTD variance whose base is the summed daily series.
    goal = {
        "demand": fm.get("demand"),
        "spend": fm.get("spend"),
        "new_customers": fm.get("new_customers"),
        "nc_demand": fm.get("nc_demand"),
        "mer": dtc._ratio(fm.get("demand"), fm.get("spend")),
        # The month is closed, so the pacing tool's MTD forecast ratio IS the month's
        # (summed daily NC demand / summed daily spend, with the sheet's NC RoAS
        # column as its fallback) — the same base the month block used all month.
        "nc_roas": fc.get("nc_roas"),
        "cac": dtc._ratio(fm.get("spend"), fm.get("new_customers")),
    }
    lines += revenue_block(mtd, goal)
    lines += acquisition_block(mtd, goal)[:1]
    lines += [
        _stat(
            _light(_chg(mtd.get("nc_demand"), goal.get("nc_demand"))),
            "NC demand",
            _vs_goal(mtd.get("nc_demand"), goal.get("nc_demand"), _k),
        ),
        _stat(
            _light(_chg(mtd.get("new_customers"), goal.get("new_customers"))),
            "New customers",
            _vs_goal(mtd.get("new_customers"), goal.get("new_customers"), _n),
        ),
    ]
    lines.append(
        f"{_n(mtd['orders'])} orders · AOV {_money(mtd['aov'])} · blended MER "
        f"{_x(mtd['mer'])} · platform ROAS {_x(mtd['platform_roas'])}"
    )

    # Finance tie-out: the certified figure, after refunds, beside the plan-basis demand.
    month_rows = (d.get("month") or {}).get("rows") or []
    if month_rows:
        m = _sum_rows(month_rows)
        lines += [
            "",
            f"**Net revenue (certified, after refunds)** {_k(m['admin_net_revenue'])} · "
            f"{_pct(_pct_change(m['admin_net_revenue'], mtd['demand']))} vs total revenue, the "
            f"gap being shipping and refunds",
        ]

    lines += [
        "",
        f"**vs last year** revenue {_pct(ly['demand_change_pct'])} · new customers "
        f"{_pct(ly['new_customers_change_pct'])} · spend {_pct(ly['spend_change_pct'])} "
        f"(full LY month {_k(lym['demand'])}) · **vs prior month** {_pct(pm['demand_change_pct'])}",
    ]
    daily = p.get("daily") or []
    if daily:
        best = max(daily, key=lambda r: r["demand"])
        worst = min(daily, key=lambda r: r["demand"])
        lines.append(
            f"Best day {date.fromisoformat(str(best['day'])):%a %b %-d} at {_money(best['demand'])} · "
            f"softest {date.fromisoformat(str(worst['day'])):%a %b %-d} at {_money(worst['demand'])}"
        )
    notes = notes_block(p)
    if notes:
        lines += ["", "**Notes**", *[f"• {x}" for x in notes]]
    lines += ["", footer(p, month_end)]

    # Replies: the week-by-week build, then every day.
    wk = p.get("weekly") or []
    tbl = [["Week", "Days", "Revenue", "vs goal", "vs LY", "Spend", "NCs"]]
    for w in wk:
        tbl.append(
            [
                w["week"],
                str(w["days"]),
                _money(w["demand"]),
                _pct(w.get("act_vs_fcst_pct")),
                _pct(w.get("act_vs_ly_pct")),
                _money(w["spend"]),
                _n(w["new_customers"]),
            ]
        )
    replies = [
        f"**{label} by week** (Monday-anchored; first and last are partial)\n"
        f"```\n{_table(tbl, 'lrrrrrr')}\n```"
    ]
    dt = [["Day", "Revenue", "vs goal", "vs LY", "Spend", "NCs"]]
    for r in daily:
        dd = date.fromisoformat(str(r["day"]))
        dt.append(
            [
                f"{dd:%a %-d}",
                _money(r["demand"]),
                _pct(r.get("act_vs_fcst_pct")),
                _pct(r.get("act_vs_ly_pct")),
                _money(r["spend"]),
                _n(r["new_customers"]),
            ]
        )
    replies.append(f"**{label} by day**\n```\n{_table(dt, 'lrrrrr')}\n```")
    return {"main": "\n".join(lines), "replies": replies}


def render(d: dict[str, Any]) -> dict[str, Any]:
    return {"weekly": render_weekly, "subpulse": render_subpulse, "recap": render_recap}[
        d["mode"]
    ](d)


#: `pulse` was the Thursday week-so-far brief the subscriber pulse replaced on
#: 2026-09-21. It stays accepted so a Routine or bookmark that still says
#: `--mode pulse` posts the new pulse instead of dying on an argparse error.
MODE_ALIASES = {"pulse": "subpulse"}


def build(mode: str, as_of: date | None = None, targets_path: str | None = None) -> dict[str, Any]:
    mode = MODE_ALIASES.get(mode, mode)
    targets = load_targets(targets_path) if targets_path else None
    return render(asyncio.run(gather(mode, as_of, targets=targets)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--mode",
        choices=["weekly", "subpulse", "recap", "pulse"],
        default="weekly",
        help="weekly (Mondays), subpulse (mid-week subscriber pulse), recap (the 1st). "
        "`pulse` is the retired name for subpulse and still runs it.",
    )
    ap.add_argument(
        "--as-of",
        help="YYYY-MM-DD: run as if today were this date (Central), to backtest a past Monday/Thursday/1st",
    )
    ap.add_argument(
        "--targets",
        metavar="PATH",
        help="pacing targets JSON to use instead of config/dtc_pacing_targets.json",
    )
    ap.add_argument("--json", action="store_true", help="emit {main, replies} as JSON")
    args = ap.parse_args(argv)

    out = build(args.mode, date.fromisoformat(args.as_of) if args.as_of else None, args.targets)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out["main"])
        for r in out["replies"]:
            print("\n" + "─" * 70 + "\n[threaded reply]\n")
            print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
