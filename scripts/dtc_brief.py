"""DTC performance brief from the bpd-mcp analytics tools → Slack-ready text.

The DTC counterpart of scripts/pos_brief.py, on the cadence the ecomm team asked
for in #ecommerce (weekly, not daily), in the shape of the MTD updates Corinne
already posts there (Revenue & Efficiency / Acquisition / month pacing / takeaways):

  weekly (Mondays)   the Mon–Sun week that just closed: demand, spend, MER, new
                     customers and CAC vs the prior week (demand also vs the
                     trailing 4-week average); then the month's pacing — demand,
                     spend, NCs, NC demand and NC ROAS — against the ecomm team's
                     forecast. One threaded reply: the 8-week table.
  pulse  (Thursdays) week so far (Mon–Wed) vs the same days last week, plus the
                     month pacing — the mid-week check on whether the plan needs
                     a spend or promo decision before the week closes.
  recap  (1st)       the month that just closed: final demand / spend / new
                     customers vs forecast, vs last month and vs last year, the
                     week-by-week build, best and worst days, and how the
                     warehouse reconciled to the ecomm team's own sheet actuals.

Every number comes from the server's own tools (tools/dtc.py) through its
read-only BigQuery data layer, so the brief can never disagree with what someone
gets from the MCP by hand. "Demand" is one definition everywhere in a message —
the sheet's Actual DMD (order subtotal + shipping on paid core-D2C orders), as
`demand` from the efficiency tool for the week figures and from the pacing tool
for the month — and MER is demand / spend on both. Targets come from config/dtc_pacing_targets.json (the
sheet as config — see bpd_mcp/pacing_targets.py); the brief prints the tab and
refresh date it used, and says plainly when the month has no targets loaded.

Not covered yet: subscriber health (active / additions / reductions / take
rate). fct_subscriptions is not in the registry; when it is, those lines belong
in a "Retention & Subscription Health" section between Acquisition and pacing.

Usage:
    uv run python scripts/dtc_brief.py --mode weekly
    uv run python scripts/dtc_brief.py --mode pulse
    uv run python scripts/dtc_brief.py --mode recap
    uv run python scripts/dtc_brief.py --mode weekly --as-of 2026-09-07 --json
    uv run python scripts/dtc_brief.py --mode recap --targets /tmp/pacing_targets.json
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

from bpd_mcp.pacing_targets import load_targets
from bpd_mcp.schemas import (
    DtcPacingInput,
    MarketingEfficiencyInput,
)
from bpd_mcp.tools import dtc

#: The channel the ecomm team's own updates live in.
SLACK_CHANNEL = "#ecommerce"

#: Flag thresholds for the "Watch" block: a paced series this far from plan
#: month-to-date, or a day where the warehouse and the sheet disagree this much.
WATCH_VARIANCE_PCT = 10.0
WATCH_SHEET_GAP_PCT = 5.0

TREND_WEEKS = 8


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
    """Slack traffic light on a variance vs plan, in the ecomm team's own idiom."""
    if variance_pct is None:
        return ""
    v = variance_pct if good_when_high else -variance_pct
    if v >= 0:
        return " :large_green_circle:"
    if v >= -WATCH_VARIANCE_PCT:
        return " :large_yellow_circle:"
    return " :red_circle:"


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


async def gather(
    mode: str,
    as_of: date | None = None,
    *,
    warehouse: Any = None,
    targets: Any = None,
) -> dict[str, Any]:
    """Run the tool calls one brief needs and return their JSON payloads, untouched.

    `warehouse` is an injection point for the tests (a fixture warehouse of literal
    rows); the CLI leaves it None and the server's own context is built and closed
    here. `targets` is a parsed PacingTargets, or None for the checked-in file —
    the month-end recap Routine passes a fresh export (see `--targets`) so the
    closed month's sheet actuals are there to reconcile against.
    """
    app = None
    if warehouse is None:
        from bpd_mcp.server import build_context

        app = await build_context()
        warehouse = app.warehouse
    wh = warehouse
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
            return {"mode": mode, "today": today, "pacing": pacing}

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
                "efficiency",
            )
            return {
                "mode": mode,
                "today": today,
                "week": (wk_start, wk_end),
                "pacing": pacing,
                "weeks": weeks,
            }

        # pulse: Monday of the current week through yesterday, and the same
        # weekdays one week earlier.
        wk_start = yesterday - timedelta(days=yesterday.weekday())
        cur = _ok(
            await dtc.get_marketing_efficiency(
                wh,
                MarketingEfficiencyInput(
                    grain="day", start_date=wk_start, end_date=yesterday, response_format="json"
                ),
            ),
            "efficiency (this week)",
        )
        prev = _ok(
            await dtc.get_marketing_efficiency(
                wh,
                MarketingEfficiencyInput(
                    grain="day",
                    start_date=wk_start - timedelta(days=7),
                    end_date=yesterday - timedelta(days=7),
                    response_format="json",
                ),
            ),
            "efficiency (last week)",
        )
        return {
            "mode": mode,
            "today": today,
            "wtd": (wk_start, yesterday),
            "pacing": pacing,
            "cur": cur,
            "prev": prev,
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
    return tot


def pacing_block(p: dict[str, Any]) -> list[str]:
    """The month-pacing lines, shared by all three modes."""
    s = p["summary"]
    mtd, fc, var, fm = (
        s["mtd"],
        s.get("forecast_mtd"),
        s.get("variance_vs_forecast_mtd"),
        s.get("month_forecast"),
    )
    tg, rd, rr, pvf = (
        s.get("to_go"),
        s.get("required_daily_average"),
        s["run_rate_projection"],
        s.get("projection_vs_forecast") or {},
    )
    ly, pm = s["last_year_mtd"], s["prior_month_to_date"]
    month_label = date.fromisoformat(s["complete_through"]).strftime("%B")
    done = s["days_left"] == 0
    head = (
        f"**{month_label} — final** ({s['elapsed_days']} days)"
        if done
        else f"**{month_label} pacing** (through {date.fromisoformat(s['complete_through']):%a %b %-d}, "
        f"{s['elapsed_days']} of {s['days_in_month']} days)"
    )
    lines = [head]
    if fc and var and fc.get("demand") is not None:
        lines.append(
            f"Demand {_k(mtd['demand'])} vs plan {_k(fc['demand'])} ({_pct(var['demand_pct'])})"
            f"{_light(var['demand_pct'])}"
        )
        if not done and fm and tg and rd and fm.get("demand") is not None:
            lines.append(
                f"  run-rate {_k(rr['demand'])} vs {_k(fm['demand'])} EOM forecast "
                f"({_pct(pvf.get('demand_pct'))}) · {_k(tg['demand'])} to go · "
                f"need {_money(rd['demand'])}/day over {s['days_left']} days"
            )
        elif done and fm and fm.get("demand") is not None:
            lines.append(f"  vs {_k(fm['demand'])} EOM forecast ({_pct(pvf.get('demand_pct'))})")
        sp = f"Spend {_k(mtd['spend'])}"
        if fc.get("spend") is not None:
            sp += f" vs {_k(fc['spend'])} plan ({_pct(var['spend_pct'])})"
        sp += f" · MER {_x(mtd['mer'])}"
        if fc.get("mer") is not None:
            sp += f" (plan {_x(fc['mer'])})"
        lines.append(sp)
        nc = f"New customers {_n(mtd['new_customers'])}"
        if fc.get("new_customers") is not None:
            nc += f" vs {_n(fc['new_customers'])} plan ({_pct(var['new_customers_pct'])})"
        nc += f" · CAC {_money(mtd['cac'])}"
        if fc.get("cac") is not None:
            nc += f" (plan {_money(fc['cac'])})"
        lines.append(nc)
        ncd = f"NC demand {_k(mtd['nc_demand'])}"
        if fc.get("nc_demand") is not None:
            ncd += f" vs {_k(fc['nc_demand'])} plan ({_pct(var.get('nc_demand_pct'))})"
        ncd += f" · NC ROAS {_x(mtd['nc_roas'])}"
        if fc.get("nc_roas") is not None:
            ncd += f" (target {_x(fc['nc_roas'])})"
        lines.append(ncd)
    else:
        lines.append(
            f"Demand {_k(mtd['demand'])} · spend {_k(mtd['spend'])} · MER {_x(mtd['mer'])} · "
            f"new customers {_n(mtd['new_customers'])} · CAC {_money(mtd['cac'])} · "
            f"NC demand {_k(mtd['nc_demand'])} (no forecast loaded for this month)"
        )
    lines.append(
        f"vs LY {_pct(ly['demand_change_pct'])} on demand · vs the same days last month "
        f"{_pct(pm['demand_change_pct'])}"
    )
    return lines


def settled_sheet_days(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Daily rows whose sheet actuals had time to be filled in.

    The team keys the sheet's Actual columns by hand, usually the next morning, so
    the export's last day or two is routinely partial. Comparing those rows to the
    warehouse produces a false "the sheet disagrees" flag every run. A row counts
    as settled when its day ended at least two days before the export was taken
    (`targets.source.refreshed_at`); without a parseable export date every row is
    treated as settled.
    """
    daily = p.get("daily") or []
    src = (p.get("targets") or {}).get("source") or {}
    try:
        exported = date.fromisoformat(str(src.get("refreshed_at", ""))[:10])
    except ValueError:
        return list(daily)
    cutoff = exported - timedelta(days=2)
    return [d for d in daily if date.fromisoformat(str(d["day"])) <= cutoff]


def watch_block(p: dict[str, Any]) -> list[str]:
    """Things worth a look, from the pacing response alone."""
    s = p["summary"]
    var = s.get("variance_vs_forecast_mtd") or {}
    flags: list[str] = []
    for key, label in (
        ("demand", "Demand"),
        ("new_customers", "New customers"),
        ("nc_demand", "NC demand"),
    ):
        v = var.get(f"{key}_pct")
        if v is not None and v <= -WATCH_VARIANCE_PCT:
            flags.append(f"{label} is {abs(v):.0f}% behind plan month-to-date.")
    sp = var.get("spend_pct")
    if sp is not None and abs(sp) >= WATCH_VARIANCE_PCT:
        flags.append(
            f"Spend is {abs(sp):.0f}% {'under' if sp < 0 else 'over'} plan month-to-date"
            + (" — room to scale if efficiency holds." if sp < 0 else ".")
        )
    gaps = [
        d
        for d in settled_sheet_days(p)
        if d.get("sheet_vs_warehouse_pct") is not None
        and abs(d["sheet_vs_warehouse_pct"]) >= WATCH_SHEET_GAP_PCT
    ]
    if gaps:
        worst = max(gaps, key=lambda d: abs(d["sheet_vs_warehouse_pct"]))
        flags.append(
            f"The pacing sheet's own actuals differ from the warehouse by ≥{WATCH_SHEET_GAP_PCT:.0f}% on "
            f"{len(gaps)} day(s) (worst {worst['day']}: sheet {_money(worst['sheet_actual_demand'])} vs "
            f"warehouse {_money(worst['demand'])}) — worth aligning definitions with the team."
        )
    if s.get("last_year_note"):
        flags.append(s["last_year_note"] + ".")
    t = p.get("targets") or {}
    if t.get("status") != "ok":
        flags.append(
            f"No targets loaded for this month ({t.get('status')}). {t.get('note') or ''}".strip()
        )
    return flags


def footer(p: dict[str, Any], through: date) -> str:
    """Provenance line. `through` is the brief's own window end; when the pacing
    block runs further (an off-schedule run mid-week) both dates are shown."""
    t = p.get("targets") or {}
    src = t.get("source") or {}
    paced = date.fromisoformat(p["summary"]["complete_through"])
    bits = [
        f"data through {through:%a %b %-d}"
        + (f" (pacing through {paced:%a %b %-d})" if paced != through else "")
    ]
    if t.get("status") == "ok":
        bits.append(
            f'targets: "{t.get("month_tab")}" tab of the pacing sheet, refreshed '
            f"{str(src.get('refreshed_at', '?'))[:10]}"
        )
    bits.append(
        "demand = Shopify total less tax on paid core-D2C orders (the sheet's Actual DMD); "
        "spend = Meta + Google"
    )
    return "_" + " · ".join(bits) + "_"


# --------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------


def render_weekly(d: dict[str, Any]) -> dict[str, Any]:
    wk_start, wk_end = d["week"]
    rows = sorted(d["weeks"]["rows"], key=lambda r: str(r["period"]))
    by = {str(r["period"]): r for r in rows}
    cur = by.get(str(wk_start))
    if cur is None:
        raise RuntimeError(f"no efficiency row for the week of {wk_start}")
    prev = by.get(str(wk_start - timedelta(days=7)))
    trailing = [by[k] for k in sorted(by) if k < str(wk_start)][-4:]
    avg4 = _sum_rows(trailing) if trailing else None
    avg4_demand = (avg4["demand"] / len(trailing)) if avg4 else None

    def _vs(key: str, cur_v: Any) -> str:
        prev_v = prev.get(key) if prev else None
        return f"{_pct(_pct_change(cur_v, prev_v))} WoW"

    head = f"🛒 **DTC — week of {_span(wk_start, wk_end)}**"
    lead = (
        f"Demand {_k(cur['demand'])}, {_vs('demand', cur['demand'])}"
        + (
            f", {_pct(_pct_change(cur['demand'], avg4_demand))} vs the trailing {len(trailing)}-wk avg"
            if avg4_demand
            else ""
        )
        + "."
    )
    lines = [head, lead, "", "**Revenue & Efficiency**"]
    lines.append(
        f"Spend {_k(cur['spend'])} ({_vs('spend', cur['spend'])}) · MER {_x(cur['mer_demand'])}"
        + (f" (prior week {_x(prev['mer_demand'])})" if prev else "")
        + f" · platform ROAS {_x(cur['platform_roas'])} · AOV {_money(cur['demand'] / cur['orders'] if cur['orders'] else None)}"
    )
    lines += ["", "**Acquisition**"]
    lines.append(
        f"New customers {_n(cur['new_customers'])} ({_vs('new_customers', cur['new_customers'])}) · "
        f"CAC {_money(cur['blended_cac'])}"
        + (f" (prior week {_money(prev['blended_cac'])})" if prev else "")
        + f" · {_pct(cur['new_customer_share'] * 100 if cur['new_customer_share'] is not None else None).lstrip('+')} of orders were first orders"
    )
    lines += ["", *pacing_block(d["pacing"])]
    flags = watch_block(d["pacing"])
    if flags:
        lines += ["", "**Watch**", *[f"• {f}" for f in flags]]
    lines += ["", footer(d["pacing"], wk_end)]

    # Threaded reply: the trend table.
    tbl = [["Week", "Demand", "WoW", "Spend", "MER", "NCs", "CAC", "NC share"]]
    prev_row = None
    for r in rows[-TREND_WEEKS:]:
        p_start = date.fromisoformat(str(r["period"]))
        tbl.append(
            [
                f"{p_start:%b %-d}",
                _money(r["demand"]),
                _pct(_pct_change(r["demand"], prev_row["demand"])) if prev_row else "",
                _money(r["spend"]),
                _x(r["mer_demand"]),
                _n(r["new_customers"]),
                _money(r["blended_cac"]),
                f"{r['new_customer_share'] * 100:.0f}%"
                if r.get("new_customer_share") is not None
                else "n/a",
            ]
        )
        prev_row = r
    reply = (
        f"**{len(rows[-TREND_WEEKS:])}-week trend** (Monday-anchored weeks, core-D2C paid demand)\n"
        f"```\n{_table(tbl, 'lrrrrrrr')}\n```"
    )
    return {"main": "\n".join(lines), "replies": [reply]}


def render_pulse(d: dict[str, Any]) -> dict[str, Any]:
    wk_start, through = d["wtd"]
    cur, prev = _sum_rows(d["cur"]["rows"]), _sum_rows(d["prev"]["rows"])
    days = (through - wk_start).days + 1
    head = f"🛒 **DTC — week so far** ({_span(wk_start, through)})"
    lines = [
        head,
        f"**{days} day{'' if days == 1 else 's'} in:** demand {_k(cur['demand'])} "
        f"({_pct(_pct_change(cur['demand'], prev['demand']))} vs the same days last week) · "
        f"spend {_k(cur['spend'])} ({_pct(_pct_change(cur['spend'], prev['spend']))}) · MER {_x(cur['mer'])}"
        + (f" (last week {_x(prev['mer'])})" if prev["mer"] else ""),
        f"New customers {_n(cur['new_customers'])} ({_pct(_pct_change(cur['new_customers'], prev['new_customers']))}) · "
        f"CAC {_money(cur['cac'])}"
        + (f" (last week {_money(prev['cac'])})" if prev["cac"] else ""),
    ]
    if days <= 1:
        lines.append("  ⓘ Only one day of the week has closed — read this as directional.")
    lines += ["", *pacing_block(d["pacing"])]
    flags = watch_block(d["pacing"])
    if flags:
        lines += ["", "**Watch**", *[f"• {f}" for f in flags]]
    lines += ["", footer(d["pacing"], through)]
    return {"main": "\n".join(lines), "replies": []}


def render_recap(d: dict[str, Any]) -> dict[str, Any]:
    p = d["pacing"]
    s = p["summary"]
    mtd, fm, pvf = s["mtd"], s.get("month_forecast") or {}, s.get("projection_vs_forecast") or {}
    ly, lym, pm = s["last_year_mtd"], s["last_year_month"], s["prior_month_to_date"]
    month_end = date.fromisoformat(s["complete_through"])
    label = month_end.strftime("%B %Y")
    beat = pvf.get("demand_pct")
    head = f"🛒 **DTC — {label} recap**"
    if fm.get("demand") is not None and beat is not None:
        lead = (
            f"{'**Beat.** ' if beat >= 0 else ''}Demand {_k(mtd['demand'])} vs the {_k(fm['demand'])} "
            f"forecast ({_pct(beat)}){_light(beat)} · {_pct(ly['demand_change_pct'])} vs {month_end.year - 1} · "
            f"{_pct(pm['demand_change_pct'])} vs the prior month"
        )
    else:
        lead = (
            f"Demand {_k(mtd['demand'])} · {_pct(ly['demand_change_pct'])} vs {month_end.year - 1} · "
            f"{_pct(pm['demand_change_pct'])} vs the prior month (no forecast loaded)"
        )
    lines = [head, lead, "", "**Revenue & Efficiency**"]
    fc = s.get("forecast_mtd") or {}
    # Every "vs forecast" below is against the sheet's stated month Total (`fm`),
    # the same base the demand lead uses — not the MTD variance, whose base is the
    # summed daily series and can differ from the Total row by a few percent.
    sp = f"Spend {_k(mtd['spend'])}"
    if fm.get("spend") is not None:
        sp += f" vs {_k(fm['spend'])} forecast ({_pct(_pct_change(mtd['spend'], fm['spend']))})"
    sp += f" · MER {_x(mtd['mer'])}" + (
        f" (plan {_x(fc.get('mer'))})" if fc.get("mer") is not None else ""
    )
    sp += f" · platform ROAS {_x(mtd['platform_roas'])} · AOV {_money(mtd['aov'])} · {_n(mtd['orders'])} orders"
    lines.append(sp)
    lines += ["", "**Acquisition**"]
    nc = f"New customers {_n(mtd['new_customers'])}"
    if fm.get("new_customers") is not None:
        nc_var = _pct_change(mtd["new_customers"], fm["new_customers"])
        nc += f" vs {_n(fm['new_customers'])} goal ({_pct(nc_var)}){_light(nc_var)}"
    nc += f" · CAC {_money(mtd['cac'])}" + (
        f" (plan {_money(fc.get('cac'))})" if fc.get("cac") is not None else ""
    )
    lines.append(nc)
    ncd = f"NC demand {_k(mtd['nc_demand'])}"
    if fm.get("nc_demand") is not None:
        ncd_var = _pct_change(mtd["nc_demand"], fm["nc_demand"])
        ncd += f" vs {_k(fm['nc_demand'])} goal ({_pct(ncd_var)}){_light(ncd_var)}"
    ncd += f" · NC ROAS {_x(mtd['nc_roas'])}" + (
        f" (target {_x(fc.get('nc_roas'))})" if fc.get("nc_roas") is not None else ""
    )
    lines.append(ncd)
    lines.append(
        f"vs last year: demand {_pct(ly['demand_change_pct'])}, new customers "
        f"{_pct(ly['new_customers_change_pct'])}, spend {_pct(ly['spend_change_pct'])} "
        f"(full LY month {_k(lym['demand'])})"
    )

    # Best / worst days from the daily rows.
    daily = p.get("daily") or []
    if daily:
        best = max(daily, key=lambda r: r["demand"])
        worst = min(daily, key=lambda r: r["demand"])
        lines += [
            "",
            f"Best day {date.fromisoformat(str(best['day'])):%a %b %-d} at {_money(best['demand'])}; "
            f"softest {date.fromisoformat(str(worst['day'])):%a %b %-d} at {_money(worst['demand'])}.",
        ]

    # Reconciliation to the sheet's own actuals, where the config carries them.
    recon = [r for r in settled_sheet_days(p) if r.get("sheet_actual_demand")]
    if recon:
        gaps = [
            r["sheet_vs_warehouse_pct"]
            for r in recon
            if r.get("sheet_vs_warehouse_pct") is not None
        ]
        if gaps:
            avg_gap = sum(gaps) / len(gaps)
            worst_r = max(recon, key=lambda r: abs(r.get("sheet_vs_warehouse_pct") or 0))
            lines += [
                "",
                f"_Reconciliation: the pacing sheet's own actuals ran {avg_gap:+.1f}% vs the warehouse on "
                f"average over {len(gaps)} days (widest {date.fromisoformat(str(worst_r['day'])):%b %-d}: "
                f"{_pct(worst_r['sheet_vs_warehouse_pct'])})._",
            ]
    flags = [f for f in watch_block(p) if not f.startswith("Spend is")]
    if flags:
        lines += ["", "**Notes**", *[f"• {f}" for f in flags]]
    lines += ["", footer(p, month_end)]

    # Reply: the week-by-week build.
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
    reply = f"**{label} by week** (Monday-anchored; first and last are partial)\n```\n{_table(tbl, 'lrrrrrr')}\n```"
    return {"main": "\n".join(lines), "replies": [reply]}


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
        help=(
            "pacing targets JSON to use instead of config/dtc_pacing_targets.json — the "
            "month-end recap passes one regenerated from a same-morning sheet export so the "
            "closed month's sheet actuals are present to reconcile against"
        ),
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
