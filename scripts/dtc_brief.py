"""DTC performance brief from the bpd-mcp analytics tools → Slack-ready text.

The DTC counterpart of scripts/pos_brief.py, on the cadence the ecomm team asked
for in #ecommerce (weekly, not daily). Three modes, each a `main` message plus
threaded `replies`:

  weekly (Mondays)   the Mon–Sun week that just closed, scored against the
                     team's plan for those seven days, with WoW and the trailing
                     4-week average; then the month to date, scored the same way.
                     Replies: the week day by day; the 8-week trend.
  pulse  (Thursdays) the week so far (Mon–yesterday) vs plan and vs the same
                     weekdays last week, then the month to date. Reply: the days
                     so far.
  recap  (1st)       the month that just closed vs its forecast, last month and
                     last year, with the certified net revenue for the P&L
                     tie-out. Replies: by week; by day.

Every line that has a plan gets a traffic light — 🟢 at or above plan, 🟡 within
WATCH_VARIANCE_PCT below it, 🔴 beyond that; cost metrics (CAC) read the other
way; spend is ⚪ because under-plan spend is a decision, not a miss. One stat
per line, light first, so the message scans as a scorecard rather than prose.

WHERE THE NUMBERS COME FROM. Every actual is BigQuery, through the server's own
tools (tools/dtc.py): `bpd_get_dtc_pacing` for the month, `bpd_get_marketing_
efficiency` for weeks and days. The brief can therefore never disagree with what
someone gets from the MCP by hand. The ecomm team's pacing sheet contributes
targets and nothing else — config/dtc_pacing_targets.json, regenerated monthly
(see bpd_mcp/pacing_targets.py). "Demand" is the sheet's plan basis: net sales
after discounts plus shipping (Shopify total less tax) on paid core-D2C orders,
one row per order; MER is demand / spend on the same basis.

Not covered yet: subscriber health (active / additions / reductions / take
rate). fct_subscriptions is not in the registry; when it is, those lines belong
in a "Retention" section between Acquisition and the month block.

Usage:
    uv run python scripts/dtc_brief.py --mode weekly
    uv run python scripts/dtc_brief.py --mode pulse
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
)
from bpd_mcp.tools import dtc

#: The channel the ecomm team's own updates live in.
SLACK_CHANNEL = "#ecommerce"

#: The yellow band: a paced series this far below plan is 🟡, beyond it 🔴.
WATCH_VARIANCE_PCT = 10.0

TREND_WEEKS = 8

GREEN, YELLOW, RED, NEUTRAL = "🟢", "🟡", "🔴", "⚪"

#: The plan series the sheet carries per day, and the two ratios derived from them.
PLAN_FIELDS = ("forecast_demand", "forecast_spend", "forecast_new_customers")


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
_pct = dtc._pct
_pct_change = dtc._pct_change


def _x(v: Any) -> str:
    return "n/a" if v is None else f"{v:.2f}x"


def _n(v: Any) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def _light(variance_pct: float | None, *, good_when_high: bool = True) -> str:
    """Traffic light on a variance vs plan; ⚪ when there is no plan to judge against."""
    if variance_pct is None:
        return NEUTRAL
    v = variance_pct if good_when_high else -variance_pct
    if v >= 0:
        return GREEN
    if v >= -WATCH_VARIANCE_PCT:
        return YELLOW
    return RED


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


def _vs_plan(actual: float | None, plan: float | None, fmt: Any, *, pct: bool = True) -> str:
    """`$63.4K vs $66.9K plan (-5.2%)`, or just the actual when there is no plan."""
    if plan is None:
        return str(fmt(actual))
    s = f"{fmt(actual)} vs {fmt(plan)} plan"
    if pct:
        s += f" ({_pct(_pct_change(actual, plan))})"
    return s


# --------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------


def plan_for(targets: PacingTargets | None, start: date, end: date) -> dict[str, float | None]:
    """The team's plan for a span of days: the daily forecasts summed, plus the
    implied MER and CAC. A series is None unless EVERY day in the span carries it —
    a week straddling a month whose tab has not landed has no plan, not half of one."""
    out: dict[str, float | None] = dict.fromkeys(("demand", "spend", "new_customers"))
    if targets is None:
        out.update(mer=None, cac=None)
        return out
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    for fld, key in zip(PLAN_FIELDS, ("demand", "spend", "new_customers"), strict=True):
        vals: list[float] = []
        for d in days:
            m = targets.month(d.strftime("%Y-%m"))
            v = m.days[d].get(fld) if m is not None and d in m.days else None
            if v is None:
                vals = []
                break
            vals.append(v)
        out[key] = sum(vals) if vals else None
    out["mer"] = dtc._ratio(out["demand"], out["spend"])
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
                    DtcPacingInput(as_of=today, month=ym, response_format="json"),
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

        pacing = _ok(
            await dtc.get_dtc_pacing(
                wh, DtcPacingInput(as_of=today, response_format="json"), targets=targets
            ),
            "pacing",
        )
        if mode == "weekly":
            wk_start, wk_end = last_complete_week(today)
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
                "targets": targets,
            }

        # pulse: Monday of the current week through yesterday, and the same
        # weekdays one week earlier.
        wk_start = yesterday - timedelta(days=yesterday.weekday())
        wk = timedelta(days=7)
        return {
            "mode": mode,
            "today": today,
            "wtd": (wk_start, yesterday),
            "pacing": pacing,
            "days": await _days(wh, wk_start, yesterday, "efficiency (this week)"),
            "prev_days": await _days(wh, wk_start - wk, yesterday - wk, "efficiency (last week)"),
            "plan": plan_for(targets, wk_start, yesterday),
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
        ("spend", "orders", "new_customers", "demand", "gross_sales", "admin_net_revenue"), 0.0
    )
    for r in rows:
        for k in tot:
            tot[k] += float(r.get(k) or 0)
    tot["mer"] = tot["demand"] / tot["spend"] if tot["spend"] else None
    tot["cac"] = tot["spend"] / tot["new_customers"] if tot["new_customers"] else None
    tot["aov"] = tot["demand"] / tot["orders"] if tot["orders"] else None
    tot["nc_share"] = tot["new_customers"] / tot["orders"] if tot["orders"] else None
    return tot


def scorecard(
    cur: dict[str, Any],
    plan: dict[str, Any],
    *,
    demand_extra: str = "",
    compare: dict[str, Any] | None = None,
    compare_label: str = "",
    prev_label: str = "",
    spend_extra: str = "",
) -> list[str]:
    """Five stat lines — demand, MER, new customers, CAC, spend — each scored
    against `plan` and, where `compare` is given, set beside that window too:
    volumes as a % change (`compare_label`, e.g. WoW), rates as the earlier level
    (`prev_label`, e.g. "prior week 3.05x")."""

    def vs(key: str, fmt: Any) -> str:
        if compare is None:
            return ""
        return f" · {_pct(_pct_change(cur.get(key), compare.get(key)))} {compare_label}"

    def was(key: str, fmt: Any) -> str:
        if compare is None or compare.get(key) is None:
            return ""
        return f" · {prev_label or compare_label} {fmt(compare[key])}"

    lines = [
        _stat(
            _light(_pct_change(cur["demand"], plan.get("demand"))),
            "Demand",
            _vs_plan(cur["demand"], plan.get("demand"), _k) + vs("demand", _k) + demand_extra,
        ),
        _stat(
            _light(_pct_change(cur["mer"], plan.get("mer"))),
            "MER",
            _vs_plan(cur["mer"], plan.get("mer"), _x, pct=False) + was("mer", _x),
        ),
        _stat(
            _light(_pct_change(cur["new_customers"], plan.get("new_customers"))),
            "New customers",
            _vs_plan(cur["new_customers"], plan.get("new_customers"), _n) + vs("new_customers", _n),
        ),
        _stat(
            _light(_pct_change(cur["cac"], plan.get("cac")), good_when_high=False),
            "CAC",
            _vs_plan(cur["cac"], plan.get("cac"), _money, pct=False) + was("cac", _money),
        ),
        _stat(
            NEUTRAL,
            "Spend",
            _vs_plan(cur["spend"], plan.get("spend"), _k) + vs("spend", _k) + spend_extra,
        ),
    ]
    return lines


def month_block(p: dict[str, Any]) -> list[str]:
    """The month to date, scored against the sheet's plan; `final` shape once closed."""
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
    demand_extra = ""
    if not done and fm.get("demand") is not None:
        demand_extra = (
            f" · run-rate {_k(rr['demand'])} vs {_k(fm['demand'])} forecast "
            f"({_pct(pvf.get('demand_pct'))})"
        )
    sp = fc.get("spend")
    spend_extra = ""
    if sp and mtd["spend"] < sp * (1 - WATCH_VARIANCE_PCT / 100):
        spend_extra = " · room to scale if efficiency holds"
    plan = {
        "demand": fc.get("demand"),
        "spend": fc.get("spend"),
        "new_customers": fc.get("new_customers"),
        "mer": fc.get("mer"),
        "cac": fc.get("cac"),
    }
    lines = [head, *scorecard(mtd, plan, demand_extra=demand_extra, spend_extra=spend_extra)]
    ncd = _vs_plan(mtd["nc_demand"], fc.get("nc_demand"), _k)
    if fc.get("nc_roas") is not None:
        ncd += f" · NC ROAS {_x(mtd['nc_roas'])} vs {_x(fc['nc_roas'])} target"
    else:
        ncd += f" · NC ROAS {_x(mtd['nc_roas'])}"
    lines.insert(
        4, _stat(_light(_pct_change(mtd["nc_demand"], fc.get("nc_demand"))), "NC demand", ncd)
    )
    tail = []
    if not done and tg.get("demand") is not None and rd.get("demand") is not None:
        tail.append(f"To go {_k(tg['demand'])} over {s['days_left']} days ({_k(rd['demand'])}/day)")
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
            f"No plan loaded for this month ({t.get('status')}); the month lines show actuals "
            f"only. {t.get('note') or ''}".strip()
        )
    return notes


def footer(p: dict[str, Any], through: date) -> str:
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
            f'plan: "{t.get("month_tab")}" tab of the pacing sheet, refreshed '
            f"{str(src.get('refreshed_at', '?'))[:10]}"
        )
    bits.append(
        "demand = net sales + shipping (Shopify total less tax) on paid core-D2C orders; "
        "MER = demand / spend; spend = Meta + Google"
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
    """Day-by-day rows for a span, with the plan and LY per day and a total row."""
    shift = timedelta(days=dtc.LY_SHIFT_DAYS)
    tbl = [["Day", "Demand", "vs plan", "vs LY", "Spend", "NCs", "CAC"]]
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
    demand_extra = (
        f" · {_pct(_pct_change(cur['demand'], avg4))} vs {len(trailing)}-wk avg" if avg4 else ""
    )

    lines = [f"🛒 **DTC — week of {_span(wk_start, wk_end)}**", "", "**Week vs plan**"]
    lines += scorecard(
        cur,
        d["plan"],
        demand_extra=demand_extra,
        compare=prev,
        compare_label="WoW",
        prev_label="prior week",
    )
    share = f"{cur['nc_share'] * 100:.0f}%" if cur["nc_share"] is not None else "n/a"
    lines.append(
        f"Platform ROAS {_x(cur_row.get('platform_roas'))} · AOV {_money(cur['aov'])} · "
        f"{share} of orders were first orders"
    )
    lines += ["", *month_block(d["pacing"])]
    notes = notes_block(d["pacing"])
    if notes:
        lines += ["", "**Notes**", *[f"• {n}" for n in notes]]
    lines += ["", footer(d["pacing"], wk_end)]

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
    tbl = [["Week", "Demand", "WoW", "Spend", "MER", "NCs", "CAC", "NC share"]]
    p_row = None
    for r in rows[-TREND_WEEKS:]:
        p_start = date.fromisoformat(str(r["period"]))
        tbl.append(
            [
                f"{p_start:%b %-d}",
                _money(r["demand"]),
                _pct(_pct_change(r["demand"], p_row["demand"])) if p_row else "",
                _money(r["spend"]),
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
        f"```\n{_table(tbl, 'lrrrrrrr')}\n```"
    )
    return {"main": "\n".join(lines), "replies": replies}


def render_pulse(d: dict[str, Any]) -> dict[str, Any]:
    wk_start, through = d["wtd"]
    cur = _sum_rows(list(d["days"].values()))
    prev = _sum_rows(list(d["prev_days"].values()))
    n = (through - wk_start).days + 1
    lines = [
        f"🛒 **DTC — week so far** ({_span(wk_start, through)})",
        "",
        f"**{n} day{'' if n == 1 else 's'} in, vs plan** · WoW is the same weekdays last week",
    ]
    lines += scorecard(cur, d["plan"], compare=prev, compare_label="WoW", prev_label="last week")
    if n <= 1:
        lines.append("ⓘ Only one day of the week has closed — read this as directional.")
    lines += ["", *month_block(d["pacing"])]
    notes = notes_block(d["pacing"])
    if notes:
        lines += ["", "**Notes**", *[f"• {x}" for x in notes]]
    lines += ["", footer(d["pacing"], through)]
    replies = [
        daily_table(
            wk_start,
            through,
            d["days"],
            None,
            d.get("targets"),
            title=f"Day by day — {_span(wk_start, through)}",
        )
    ]
    return {"main": "\n".join(lines), "replies": replies}


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
    plan = {
        "demand": fm.get("demand"),
        "spend": fm.get("spend"),
        "new_customers": fm.get("new_customers"),
        "mer": dtc._ratio(fm.get("demand"), fm.get("spend")),
        "cac": dtc._ratio(fm.get("spend"), fm.get("new_customers")),
    }
    lines += scorecard(mtd, plan)
    ncd = _vs_plan(mtd["nc_demand"], fm.get("nc_demand"), _k)
    if fc.get("nc_roas") is not None:
        ncd += f" · NC ROAS {_x(mtd['nc_roas'])} vs {_x(fc['nc_roas'])} target"
    lines.insert(
        7, _stat(_light(_pct_change(mtd["nc_demand"], fm.get("nc_demand"))), "NC demand", ncd)
    )
    lines.append(
        f"{_n(mtd['orders'])} orders · AOV {_money(mtd['aov'])} · platform ROAS "
        f"{_x(mtd['platform_roas'])}"
    )

    # Finance tie-out: the certified figure, after refunds, beside the plan-basis demand.
    month_rows = (d.get("month") or {}).get("rows") or []
    if month_rows:
        m = _sum_rows(month_rows)
        lines += [
            "",
            f"**Net revenue (certified, after refunds)** {_k(m['admin_net_revenue'])} · "
            f"{_pct(_pct_change(m['admin_net_revenue'], mtd['demand']))} vs demand, the gap "
            f"being shipping and refunds",
        ]

    lines += [
        "",
        f"**vs last year** demand {_pct(ly['demand_change_pct'])} · new customers "
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
    tbl = [["Week", "Days", "Demand", "vs plan", "vs LY", "Spend", "NCs"]]
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
    dt = [["Day", "Demand", "vs plan", "vs LY", "Spend", "NCs"]]
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
    return {"weekly": render_weekly, "pulse": render_pulse, "recap": render_recap}[d["mode"]](d)


def build(mode: str, as_of: date | None = None, targets_path: str | None = None) -> dict[str, Any]:
    targets = load_targets(targets_path) if targets_path else None
    return render(asyncio.run(gather(mode, as_of, targets=targets)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=["weekly", "pulse", "recap"], default="weekly")
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
