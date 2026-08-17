"""Target POS brief from BigQuery → Slack-ready text.

Two modes, matching the cadence leadership reads:

  weekly (Mondays)  full recap of the week that just closed: headline, 4-week
                    metric table, what's working / what to watch, top SKUs vs
                    $PSPW goal, category mix. Plus two threaded replies with
                    the full SKU detail (velocity/goal, then units/in-stock).
  pulse  (Thursdays) week-so-far check: WTD vs the same point last week and vs
                    the trailing 4-week pace, movers, and any in-stock flag
                    worth acting on before the week closes.

Data source is `biom_canvas` in BigQuery via the read-only
`claude-code-bq-readonly` service account. That credential reads the whole
warehouse, but the direct customer identifiers — dim_customer.{email,
first_name, last_name, phone} and bdg_customer_identity.normalized_email — are
behind a `biom-pii/direct-identifier` policy tag it deliberately cannot read.
Customer analysis works off the pseudonymous `customer_id`; nothing here needs a
name or an email. Note this makes `SELECT *` fail on those two tables.

$PSPW goals are NOT in BigQuery (KMG publishes them); they live in
config/pspw_goals.json and the brief prints that file's vintage.

Usage:
    python scripts/pos_brief.py --mode weekly
    python scripts/pos_brief.py --mode pulse
    python scripts/pos_brief.py --mode weekly --week-ending 2026-08-01
    python scripts/pos_brief.py --mode weekly --json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT = "biom-reporting-s26"
LOCATION = "us-central1"
DS = f"`{PROJECT}.biom_canvas."
SALES = f"{DS}fct_target_sales`"
INV = f"{DS}fct_target_inventory`"
PROD = f"{DS}dim_product`"
GOALS_PATH = Path(__file__).resolve().parents[1] / "config" / "pspw_goals.json"

# The feed carries a long tail of residual DPCIs — discontinued items, test
# sites, returns-only rows — that config/pspw_goals.json does not name. At w/e
# 2026-08-01 there were 17 of them worth $44 combined. They are dropped from the
# SKU tables above this threshold of materiality, but a DPCI selling more than
# this is surfaced by name: a genuinely new item must not be able to hide in the
# suppressed tail.
NEW_SKU_MIN = 500.0

# Target's published in-stock goals. Also the line between a SKU that earns its
# own callout and one that belongs in the trailing roll-up.
OOS_GOAL = 5.0
WIP_GOAL = 94.0

# Category rollup used in leadership reporting: dispensers sit with the
# cleaning franchise, Baby is broken out of Personal Care. Verified to
# reproduce the 8/3 published mix exactly.
CATEGORY_SQL = """
  CASE WHEN p.sub = 'Baby' THEN 'Baby'
       WHEN p.cat IN ('Dispensers', 'Cleaning') THEN 'Cleaning'
       ELSE 'Personal Care' END
"""
PROD_CTE = f"""
  prod AS (
    SELECT SAFE_CAST(tcin AS INT64) AS tcin,
           ANY_VALUE(product_category) AS cat,
           ANY_VALUE(product_sub_category) AS sub
    FROM {PROD}
    WHERE is_current = TRUE AND tcin IS NOT NULL
    GROUP BY 1
  )
"""


# ---------- plumbing ----------


def _client():
    from google.cloud import bigquery

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        b64 = os.environ.get("GCP_SA_KEY_B64")
        if b64:
            dest = Path.home() / ".config" / "gcloud" / "biom-bq-sa.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(b64))
            dest.chmod(0o600)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(dest)
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _q(client, sql: str) -> list[Any]:
    """Run SQL and return MUTABLE row objects (BigQuery Rows are read-only,
    and the renderers annotate rows with derived fields like pspw)."""
    from types import SimpleNamespace

    return [SimpleNamespace(**dict(r.items())) for r in client.query(sql).result()]


def _goals() -> dict[str, Any]:
    """KMG reference data: goals, POG-authorized doors, and display names."""
    return json.loads(GOALS_PATH.read_text())


def _annotate(skus: list[Any], cfg: dict[str, Any]) -> list[Any]:
    """Attach KMG display name / POG doors / goal, and derive $PSPW.

    POG doors come from KMG when known: inventory-derived counts include stores
    holding stock without POG authorization, which inflates the denominator and
    understates $PSPW (1,726 vs 1,598 on Disinf 180ct = $25.23 vs $27.32).
    """
    ref = {**cfg["assortment"], **cfg["excluded"]}
    for s in skus:
        meta = ref.get(s.dpci, {})
        s.name = meta.get("name") or (s.descr or s.dpci or "—")[:30]
        s.goal = meta.get("goal_pspw")
        # An explicit null pog_doors means KMG authorizes no planogram doors —
        # online-only or de-listed. Per-door velocity is undefined there, so it
        # renders as "—" rather than dividing by the handful of stores that
        # happen to hold stock. Only DPCIs absent from the file fall back to the
        # inventory door count, since that is all we have for a new item.
        s.doors_pog = meta.get("pog_doors") if s.dpci in ref else getattr(s, "doors", None)
        s.in_assortment = s.dpci in cfg["assortment"]
        s.known = s.dpci in ref
        s.pspw = (s.amt / s.doors_pog) if s.doors_pog else None
        s.upspw = (s.units / s.doors_pog) if s.doors_pog else None
        s.pct_goal = (s.pspw / s.goal * 100) if (s.goal and s.pspw) else None
    return skus


def _pct(now: float | None, before: float | None, digits: int = 1) -> str:
    if not before or now is None:
        return "n/a"
    pct = (now - before) / before * 100
    if round(pct, digits) == 0:  # never print "-0.0%"
        pct = 0.0
    return f"{pct:+.{digits}f}%"


def _fiscal_label(week_end: date) -> str:
    """Week label matching KMG/leadership convention, e.g. "Jul W4 '26".

    The week is named for the month it STARTS in (Sunday), numbered by that
    Sunday's ordinal position among Sundays in that month. w/e Sat Aug 1 2026
    starts Sun Jul 26 — the 4th Sunday of July — hence "Jul W4 '26", which is
    how the published posts label it (labeling by week-END would say Aug W1).
    """
    start = week_end - timedelta(days=6)
    first = start.replace(day=1)
    # Sunday index within the start month.
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    week_no = (start - first_sunday).days // 7 + 1
    return f"{start.strftime('%b')} W{week_no} '{start.strftime('%y')}"


def _latest_week_end(sales_through: date) -> date:
    """Newest week-ending Saturday the feed could cover.

    NOT "the Saturday before the current week": when the feed has caught up
    through a Saturday, that Saturday's week has closed and is the one to
    report. Backing off a further week would have the Monday brief headline
    numbers a week stale — and repost the previous week's brief verbatim.

    The caller still pins the reporting week to the newest week that actually
    closed COMPLETE, so a Saturday whose week is missing days falls back.
    """
    return sales_through - timedelta(days=(sales_through.weekday() - 5) % 7)


# ---------- queries ----------


def feed_bounds(client) -> Any:
    """Freshness of each feed, plus where the sales feed changed grain.

    `first_daily` is derived, not hardcoded: Target's sales feed used to deliver
    one row per week stamped on the Saturday, and switched to true daily rows
    partway through the history. Every pre-switch row lands on a Saturday, so
    the first non-Saturday date is the cutover.
    """
    return _q(
        client,
        f"""SELECT (SELECT MAX(sales_date) FROM {SALES} WHERE is_current)  AS sales_through,
                   (SELECT MAX(inventory_date) FROM {INV} WHERE is_current) AS inv_through,
                   (SELECT MIN(sales_date) FROM {SALES}
                     WHERE is_current AND EXTRACT(DAYOFWEEK FROM sales_date) != 7)
                                                                            AS first_daily""",
    )[0]


def _complete(row: Any, first_daily: date) -> bool:
    """Does this week carry a full week of sales?

    Before the cutover a complete week is the ONE Saturday-stamped row that
    holds the whole week; after it, seven dated rows. The cutover week itself
    straddles the two and is genuinely short — w/e 2026-05-09 holds 4 of 7 days,
    reading $183.9K against neighbours near $330K. Averaging that in dragged the
    13-week average down by ~$11K/wk, so short weeks are dropped from every
    table and average instead of silently diluting them.
    """
    return row.days >= (1 if row.week_end < first_daily else 7)


def weekly_series(client, week_end: date, weeks: int = 17) -> list[Any]:
    """One row per week, newest first, ending at `week_end`.

    Fetches more weeks than the brief reports so that dropping short weeks
    still leaves a full 13 complete ones to average over.
    """
    start = week_end - timedelta(days=7 * weeks - 1)
    return _q(
        client,
        f"""
        SELECT DATE_ADD(DATE_TRUNC(sales_date, WEEK(SUNDAY)), INTERVAL 6 DAY) AS week_end,
               SUM(sale_amount) AS amt,
               SUM(sale_quantity) AS units,
               SUM(promo_sale_amount) AS promo_amt,
               SUM(IF(origination_channel != 'STORE', sale_amount, 0)) AS online_amt,
               COUNT(DISTINCT location_id) AS doors,
               COUNT(DISTINCT sales_date) AS days
        FROM {SALES}
        WHERE is_current AND sales_date BETWEEN '{start}' AND '{week_end}'
        GROUP BY 1
        HAVING week_end <= '{week_end}'
        ORDER BY week_end DESC
        """,
    )


def record_high(client, week_end: date, first_daily: date) -> float:
    """Best complete week on record through `week_end`.

    Scanned over all history, not just the 13 weeks in the table, so calling a
    week a record is a claim the reader can trust.
    """
    rows = _q(
        client,
        f"""
        SELECT DATE_ADD(DATE_TRUNC(sales_date, WEEK(SUNDAY)), INTERVAL 6 DAY) AS week_end,
               SUM(sale_amount) AS amt,
               COUNT(DISTINCT sales_date) AS days
        FROM {SALES}
        WHERE is_current AND sales_date <= '{week_end}'
        GROUP BY 1
        HAVING week_end <= '{week_end}'
        """,
    )
    good = [r.amt for r in rows if _complete(r, first_daily)]
    return max(good) if good else 0.0


def weekly_inventory(client, week_ends: list[date]) -> dict[date, Any]:
    """Week-end inventory snapshot: EOH+OW, quantity-weighted WIP/OOS."""
    in_list = ", ".join(f"'{d}'" for d in week_ends)
    rows = _q(
        client,
        f"""
        SELECT inventory_date,
               SUM(ending_on_hand_q + ending_on_transfer_q) AS eoh_ow,
               SAFE_DIVIDE(SUM(instock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS wip,
               SAFE_DIVIDE(SUM(out_of_stock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS oos
        FROM {INV}
        WHERE is_current AND inventory_date IN ({in_list})
        GROUP BY 1
        """,
    )
    return {r.inventory_date: r for r in rows}


def sku_detail(client, week_end: date) -> list[Any]:
    """Per-SKU sales + inventory for `week_end`, with prior-week comparison."""
    wk_start = week_end - timedelta(days=6)
    pw_end, pw_start = week_end - timedelta(days=7), week_end - timedelta(days=13)
    return _q(
        client,
        f"""
        WITH {PROD_CTE},
        cur AS (
          SELECT dpci, ANY_VALUE(item_description) AS descr,
                 SUM(sale_amount) AS amt, SUM(sale_quantity) AS units,
                 ANY_VALUE(tcin) AS tcin
          FROM {SALES}
          WHERE is_current AND sales_date BETWEEN '{wk_start}' AND '{week_end}'
          GROUP BY dpci
        ),
        prev AS (
          SELECT dpci, SUM(sale_amount) AS amt
          FROM {SALES}
          WHERE is_current AND sales_date BETWEEN '{pw_start}' AND '{pw_end}'
          GROUP BY dpci
        ),
        inv AS (
          SELECT dpci,
                 COUNT(DISTINCT location_id) AS doors,
                 SUM(ending_on_hand_q + ending_on_transfer_q) AS eoh_ow,
                 SAFE_DIVIDE(SUM(instock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS wip,
                 SAFE_DIVIDE(SUM(out_of_stock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS oos
          FROM {INV}
          WHERE is_current AND inventory_date = '{week_end}'
          GROUP BY dpci
        ),
        inv_prev AS (
          SELECT dpci,
                 SUM(ending_on_hand_q + ending_on_transfer_q) AS eoh_ow,
                 SAFE_DIVIDE(SUM(out_of_stock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS oos
          FROM {INV}
          WHERE is_current AND inventory_date = '{pw_end}'
          GROUP BY dpci
        )
        SELECT cur.dpci, cur.descr, cur.amt, cur.units, prev.amt AS prev_amt,
               inv.doors, inv.eoh_ow, inv.wip, inv.oos,
               inv_prev.oos AS prev_oos, inv_prev.eoh_ow AS prev_eoh_ow,
               {CATEGORY_SQL} AS category
        FROM cur
        LEFT JOIN prev USING (dpci)
        LEFT JOIN inv USING (dpci)
        LEFT JOIN inv_prev USING (dpci)
        LEFT JOIN prod p ON p.tcin = cur.tcin
        ORDER BY cur.amt DESC
        """,
    )


def category_mix(client, week_end: date) -> list[Any]:
    wk_start = week_end - timedelta(days=6)
    return _q(
        client,
        f"""
        WITH {PROD_CTE}
        SELECT {CATEGORY_SQL} AS grp, SUM(s.sale_amount) AS amt
        FROM {SALES} s LEFT JOIN prod p ON p.tcin = s.tcin
        WHERE s.is_current AND s.sales_date BETWEEN '{wk_start}' AND '{week_end}'
        GROUP BY 1 ORDER BY amt DESC
        """,
    )


def wtd(client, through: date) -> dict[str, Any]:
    """Week-to-date vs the SAME weekday span in each of the prior four weeks.

    Comparing a partial week against a flat daily average overstates it whenever
    the elapsed days skew to a weekend — Target's Saturday runs well above its
    Monday. Every comparison here therefore covers the identical Sun..N span,
    shifted back one week at a time, so the pace figure is like-for-like.
    """
    wk_start = through - timedelta(days=(through.weekday() + 1) % 7)
    days = (through - wk_start).days + 1
    rows = _q(
        client,
        f"""
        WITH spans AS (
          SELECT wk_ago,
                 DATE_SUB(DATE '{wk_start}', INTERVAL wk_ago WEEK) AS s,
                 DATE_SUB(DATE '{through}',  INTERVAL wk_ago WEEK) AS e
          FROM UNNEST([0, 1, 2, 3, 4]) AS wk_ago
        )
        SELECT sp.wk_ago,
               SUM(x.sale_amount) AS amt,
               SUM(x.sale_quantity) AS units,
               COUNT(DISTINCT x.sales_date) AS days
        FROM spans sp
        LEFT JOIN {SALES} x
          ON x.is_current AND x.sales_date BETWEEN sp.s AND sp.e
        GROUP BY 1
        ORDER BY 1
        """,
    )
    by = {r.wk_ago: r for r in rows}
    # Only prior spans with the same number of dated days are comparable; the
    # weekly-grained era would otherwise contribute a whole week as one "day".
    peers = [by[i] for i in (1, 2, 3, 4) if i in by and by[i].days == days]
    return {
        "days": days,
        "week_start": wk_start,
        "cur": by.get(0),
        "prev": by.get(1),
        "avg4_span": (sum(p.amt for p in peers) / len(peers)) if peers else None,
        "peer_weeks": len(peers),
    }


# ---------- rendering ----------


def _table(rows: list[list[str]], aligns: str) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [
            r[i].ljust(widths[i]) if aligns[i] == "l" else r[i].rjust(widths[i])
            for i in range(len(r))
        ]
        out.append(" ".join(cells).rstrip())
    return "\n".join(out)


def render_weekly(d: dict[str, Any]) -> dict[str, Any]:
    wk, series, inv = d["week_end"], d["series"], d["inventory"]
    goals, skus, mix = d["goals"], d["skus"], d["mix"]
    cur = series[0]
    recent = series[:4]
    trailing = series[:13]

    avg4 = sum(r.amt for r in recent) / len(recent)
    avg13 = sum(r.amt for r in trailing) / len(trailing)
    # The current week's total and the all-time high are summed by separate
    # queries over different groupings, so float addition order makes them
    # differ in the 10th decimal even when they are the same week. Compare to
    # the cent, or a genuine record silently fails to announce itself.
    is_record = cur.amt >= d["record_high"] - 0.01
    streak = 0
    for i in range(len(series) - 1):
        if series[i].amt > series[i + 1].amt:
            streak += 1
        else:
            break

    head = f"🎯 **Target POS — {_fiscal_label(wk)}** (w/e {wk:%a %b %-d})"
    lead = (
        f"{'**Record week.** ' if is_record else ''}"
        f"${cur.amt / 1000:,.1f}K, {_pct(cur.amt, series[1].amt if len(series) > 1 else None)} WoW"
    )
    if streak >= 2:
        lead += f" — {streak} straight weekly gains"
    lead += (
        f". Trailing 4-wk avg ${avg4 / 1000:,.1f}K, {len(trailing)}-wk avg ${avg13 / 1000:,.1f}K."
    )

    # 4-week metric table
    labels = [_fiscal_label(r.week_end) for r in recent]
    rows = [["", *labels]]
    rows.append(["Sales $"] + [f"${r.amt:,.0f}" for r in recent])
    pspw_tot = d["pspw_by_week"]
    rows.append(["$PSPW"] + [f"${pspw_tot.get(r.week_end, 0):,.2f}" for r in recent])
    rows.append(["Units"] + [f"{int(r.units):,}" for r in recent])
    rows.append(["UPSPW"] + [f"{d['upspw_by_week'].get(r.week_end, 0):,.1f}" for r in recent])
    rows.append(
        ["Promo % of sales"]
        + [f"{r.promo_amt / r.amt * 100:,.1f}%" if r.amt else "n/a" for r in recent]
    )
    rows.append(
        ["Online orig. pen."]
        + [f"{r.online_amt / r.amt * 100:,.1f}%" if r.amt else "n/a" for r in recent]
    )
    rows.append(
        [f"OOS %  (goal {OOS_GOAL:.1f}%)"]
        + [f"{inv[r.week_end].oos:,.2f}" if r.week_end in inv else "—" for r in recent]
    )
    rows.append(
        [f"WIP %  (goal {WIP_GOAL:.0f}%)"]
        + [f"{inv[r.week_end].wip:,.1f}" if r.week_end in inv else "—" for r in recent]
    )
    rows.append(
        ["EOH+OW units"]
        + [f"{int(inv[r.week_end].eoh_ow):,}" if r.week_end in inv else "—" for r in recent]
    )
    rows.append(
        ["Weeks of supply"]
        + [
            f"{inv[r.week_end].eoh_ow / r.units:,.1f}" if r.week_end in inv and r.units else "—"
            for r in recent
        ]
    )
    metric_table = "```\n" + _table(rows, "l" + "r" * len(recent)) + "\n```"

    # goal attainment (skus are pre-annotated with name/goal/pog doors/pspw)
    goaled = [s for s in skus if s.in_assortment and s.goal]
    above = [s for s in goaled if s.pct_goal and s.pct_goal >= 100]
    below = sorted([s for s in goaled if s.pct_goal], key=lambda s: s.pct_goal)[:4]

    gainers = sorted(
        [s for s in skus if s.prev_amt and s.in_assortment],
        key=lambda s: -(s.amt / s.prev_amt),
    )[:5]
    decliners = sorted(
        [s for s in skus if s.prev_amt and s.in_assortment and s.amt < s.prev_amt],
        key=lambda s: s.amt / s.prev_amt,
    )[:3]
    # In-stock flags cover every SKU KMG knows that is actually selling — NOT
    # just the goaled assortment. HS Go-Pack 20ct carries no published $PSPW
    # goal, so scoping this to the assortment hid it breaching the 5.0% OOS goal
    # at 6.4% on $5.1K of sales. Lacking a goal is a reason to leave a SKU out of
    # the goal math, not out of the in-stock flags. The sales floor keeps the
    # de-listed tail (Terracotta: 80% OOS on 2 units) from crowding the list.
    flagged = [s for s in skus if s.oos is not None and s.known and (s.amt or 0) >= NEW_SKU_MIN]
    breaching = sorted([s for s in flagged if s.oos > OOS_GOAL], key=lambda s: -s.oos)
    # Whatever is called out individually above is not repeated in the roll-up.
    worst_oos = sorted([s for s in flagged if s.oos <= OOS_GOAL], key=lambda s: -s.oos)[:3]

    wos = inv[wk].eoh_ow / cur.units if wk in inv and cur.units else None
    wos_4ago = (
        inv[recent[-1].week_end].eoh_ow / recent[-1].units
        if recent[-1].week_end in inv and recent[-1].units
        else None
    )

    working = ["✅ **What's working**"]
    mix_tot = sum(m.amt for m in mix)
    for m in mix:
        if m.grp == "Baby":
            working.append(
                f"• **Baby is now {m.amt / mix_tot * 100:.0f}% of Target revenue** "
                f"(${m.amt / 1000:,.1f}K)."
            )
    if wk in inv:
        working.append(
            f"• **In-stock execution:** OOS {inv[wk].oos:.2f}% vs {OOS_GOAL:.1f}% goal, "
            f"WIP {inv[wk].wip:.1f}% vs {WIP_GOAL:.0f}% goal."
        )
    if wos and wos_4ago and wos < wos_4ago:
        working.append(
            f"• **Inventory is unwinding** — WOS {wos_4ago:.1f} → {wos:.1f} wks "
            "over four weeks with sales rising."
        )
    if above:
        working.append(
            "• Above $PSPW goal: " + ", ".join(f"{s.name} {s.pct_goal:.0f}%" for s in above) + "."
        )
    if gainers:
        working.append(
            "• Top WoW gainers: "
            + ", ".join(f"{g.name} {_pct(g.amt, g.prev_amt)}" for g in gainers[:5])
            + "."
        )

    watch = ["⚠️ **What to watch**"]
    # A SKU past the in-stock goal leads the section — it is the one thing here
    # someone can act on this week, and burying it in the trailing "Highest OOS"
    # roll-up reads as routine. Printing last week's rate and the cover trend
    # alongside separates a developing stockout from a one-week blip.
    for s in breaching:
        bits = [f"OOS {s.oos:.1f}%"]
        if s.prev_oos is not None:
            bits.append(f"up from {s.prev_oos:.1f}% last week")
        if s.prev_eoh_ow and s.eoh_ow:
            bits.append(f"EOH+OW {int(s.prev_eoh_ow):,} → {int(s.eoh_ow):,} units")
        if s.eoh_ow and s.units:
            bits.append(f"{s.eoh_ow / s.units:.1f} wks cover")
        watch.append(
            f"• 🔴 **{s.name} is past the {OOS_GOAL:.1f}% in-stock goal** — "
            + ", ".join(bits)
            + f" on ${s.amt:,.0f} of sales."
        )
    promo_now = cur.promo_amt / cur.amt * 100 if cur.amt else 0
    promo_then = (
        series[3].promo_amt / series[3].amt * 100 if len(series) > 3 and series[3].amt else None
    )
    if promo_then and promo_now - promo_then > 15:
        watch.append(
            f"• **Promo-assisted growth.** Promo penetration {promo_then:.0f}% → "
            f"{promo_now:.0f}% over four weeks — read the step-up as event-driven, "
            "not a new baseline."
        )
    for s in below:
        s_wos = s.eoh_ow / s.units if s.eoh_ow and s.units else None
        watch.append(
            f"• **{s.name}** — {s.pct_goal:.0f}% to goal (${s.pspw:.2f} vs "
            f"${s.goal:.2f})" + (f", {s_wos:.1f} wks supply." if s_wos else ".")
        )
    if decliners:
        watch.append(
            "• Decliners WoW: "
            + ", ".join(f"{s.name} {_pct(s.amt, s.prev_amt)}" for s in decliners)
            + "."
        )
    if worst_oos and worst_oos[0].oos and worst_oos[0].oos > 1:
        watch.append(
            "• Highest OOS: "
            + ", ".join(f"{s.name} {s.oos:.1f}% (WIP {s.wip:.1f}%)" for s in worst_oos)
            + "."
        )
    listed = [s for s in skus if s.known or (s.amt or 0) >= NEW_SKU_MIN]
    suppressed = len(skus) - len(listed)
    newcomers = [s for s in listed if not s.known]
    if newcomers:
        watch.append(
            "• **Unrecognised DPCI selling:** "
            + ", ".join(f"{s.dpci} ({s.descr or '—'}) ${s.amt:,.0f}" for s in newcomers)
            + " — not in the KMG assortment file; confirm whether it needs a "
            "$PSPW goal."
        )

    top5 = [["", "", "$PSPW", "Goal", "%Goal", "WoW"]]
    for s in sorted(goaled, key=lambda s: -s.amt)[:5]:
        top5.append(
            [
                s.name,
                f"${s.amt:,.0f}",
                f"${s.pspw:,.2f}",
                f"${s.goal:,.2f}",
                f"{s.pct_goal:.0f}%",
                _pct(s.amt, s.prev_amt),
            ]
        )
    top5_table = "```\n" + _table(top5, "lrrrrr") + "\n```"

    mix_line = "Mix: " + " · ".join(
        f"{m.grp} ${m.amt / 1000:,.1f}K ({m.amt / mix_tot * 100:.0f}%)" for m in mix
    )

    notes = [
        f"$PSPW/UPSPW = SKU sales ÷ POG-authorized doors, summed across the "
        f"{len(goals['assortment'])}-SKU goaled assortment (KMG convention; excludes the "
        f"20ct travel go-pack and online-only dispensers). Goals as published by KMG "
        f"({goals['source']}, {goals['as_of']}).",
        "OOS%/WIP% are quantity-weighted across all doors, not a per-SKU average.",
        f"Source: BigQuery `biom_canvas` — sales through {d['sales_through']}, "
        f"inventory through {d['inv_through']}. Target restates recent weeks, so "
        f"figures can move.",
    ]
    if d["dropped"]:
        notes.append(
            "Excluded from all averages as short weeks: "
            + ", ".join(f"w/e {r.week_end} ({r.days}/7 days)" for r in d["dropped"])
            + "."
        )
    if suppressed:
        notes.append(
            f"{suppressed} residual DPCIs under ${NEW_SKU_MIN:,.0f} in weekly sales "
            "omitted from the SKU tables."
        )
    foot = "_" + " ".join(notes) + "_"

    main = "\n\n".join(
        [
            head,
            lead,
            metric_table,
            "\n".join(working),
            "\n".join(watch),
            "**Top 5 SKUs — $ and % to $PSPW goal**",
            top5_table,
            mix_line,
            foot,
        ]
    )

    # ---- threaded replies: full SKU detail
    r1 = [["DPCI", "Description", "Doors", "Sales$", "$PSPW", "Goal", "%Goal", "WoW"]]
    for s in listed:
        r1.append(
            [
                s.dpci or "—",
                s.name,
                f"{s.doors_pog:,}" if s.doors_pog else "—",
                f"${s.amt:,.0f}",
                f"${s.pspw:,.2f}" if s.pspw else "—",
                f"${s.goal:,.2f}" if s.goal else "—",
                f"{s.pct_goal:.0f}%" if s.pct_goal else "—",
                _pct(s.amt, s.prev_amt),
            ]
        )
    reply1 = (
        f"**Full SKU detail — {_fiscal_label(wk)} (1 of 2): velocity & goal attainment**\n"
        "```\n" + _table(r1, "llrrrrrr") + "\n```"
    )

    r2 = [["DPCI", "Description", "Units", "UPSPW", "OOS%", "WIP%", "EOH+OW", "WOS"]]
    for s in sorted(listed, key=lambda s: -(s.units or 0)):
        wos_s = s.eoh_ow / s.units if s.eoh_ow and s.units else None
        r2.append(
            [
                s.dpci or "—",
                s.name,
                f"{int(s.units):,}",
                f"{s.upspw:,.1f}" if s.upspw else "—",
                f"{s.oos:,.2f}" if s.oos is not None else "—",
                f"{s.wip:,.1f}" if s.wip is not None else "—",
                f"{int(s.eoh_ow):,}" if s.eoh_ow else "—",
                f"{wos_s:,.1f}" if wos_s else "—",
            ]
        )
    reply2 = (
        f"**Full SKU detail — {_fiscal_label(wk)} (2 of 2): units, in-stock & cover**\n"
        "```\n" + _table(r2, "llrrrrrr") + "\n```"
    )

    return {"main": main, "replies": [reply1, reply2]}


def render_pulse(d: dict[str, Any]) -> dict[str, Any]:
    w, skus = d["wtd"], d["skus"]
    cur, prev = w["cur"], w["prev"]
    span = f"{w['week_start']:%a %b %-d}–{d['sales_through']:%a %b %-d}"
    if w["days"] == 1:
        span = f"{d['sales_through']:%a %b %-d}"

    lines = [
        f"🎯 **Target POS — week so far** ({span})",
        "",
        f"**{w['days']} day{'' if w['days'] == 1 else 's'} in:** ${cur.amt:,.0f} · "
        f"{int(cur.units):,} units",
        f"  vs the same days last week: {_pct(cur.amt, prev.amt if prev else None)} on "
        f"dollars, {_pct(cur.units, prev.units if prev else None)} on units",
    ]
    if w["avg4_span"]:
        lines.append(
            f"  vs the same days averaged over the prior {w['peer_weeks']} weeks: "
            f"${w['avg4_span']:,.0f} ({_pct(cur.amt, w['avg4_span'])})"
        )
    if w["days"] <= 2:
        lines.append(
            f"  ⓘ Only {w['days']} day{'' if w['days'] == 1 else 's'} of the week "
            f"{'has' if w['days'] == 1 else 'have'} landed in BigQuery — read this "
            "as directional."
        )
    lines += ["", "**Movers so far this week** (vs the same days last week):"]
    movers = sorted(
        [s for s in skus if s.prev_amt and s.amt and s.in_assortment],
        key=lambda s: -abs(s.amt - s.prev_amt),
    )[:5]
    for m in movers:
        arrow = "▲" if m.amt >= m.prev_amt else "▼"
        lines.append(
            f"  {arrow} {m.name} — ${m.amt:,.0f} "
            f"({m.amt - m.prev_amt:+,.0f}, {_pct(m.amt, m.prev_amt)})"
        )

    # Every KMG-known SKU that is actually selling, not just the goaled ones — a
    # SKU without a published $PSPW goal can still breach the in-stock goal. The
    # sales floor keeps out the residual tail (de-listed Terracotta, unnamed test
    # DPCIs), which sits at 75-100% OOS on a handful of units and would crowd out
    # the flags anyone can act on.
    flags = [
        s
        for s in skus
        if s.oos is not None
        and s.oos > 2
        and s.known
        and (s.amt or 0) >= NEW_SKU_MIN / 2  # partial week — lower bar
    ]
    if flags:
        lines += ["", "⚠️ **In-stock flags** (OOS > 2%):"]
        for s in sorted(flags, key=lambda s: -s.oos)[:4]:
            lines.append(f"  {s.name} — OOS {s.oos:.1f}%, WIP {s.wip:.1f}%")

    lines += [
        "",
        f"_Partial week — not comparable to a closed week. Source: BigQuery "
        f"`biom_canvas`, sales through {d['sales_through']}, inventory through "
        f"{d['inv_through']}. Target restates recent weeks._",
    ]
    return {"main": "\n".join(lines), "replies": []}


# ---------- orchestration ----------


def build(mode: str, week_ending: str | None = None, through: str | None = None) -> dict[str, Any]:
    client = _client()
    bounds = feed_bounds(client)
    sales_through, inv_through = bounds.sales_through, bounds.inv_through
    if through:
        # Backtest a pulse as it would have read on an earlier day. Roll the
        # inventory snapshot back too — pairing a past week's sales with today's
        # in-stock would quietly invent a reading that never existed.
        sales_through = min(date.fromisoformat(through), sales_through)
        inv_through = _q(
            client,
            f"""SELECT MAX(inventory_date) AS d FROM {INV}
                WHERE is_current AND inventory_date <= '{sales_through}'""",
        )[0].d
    goals = _goals()

    if mode == "weekly":
        wk = date.fromisoformat(week_ending) if week_ending else _latest_week_end(sales_through)
        raw = [r for r in weekly_series(client, wk) if r.week_end <= wk]
        series = [r for r in raw if _complete(r, bounds.first_daily)]
        dropped = [r for r in raw if not _complete(r, bounds.first_daily)]
        if not week_ending and series:
            # Report the newest week that actually closed complete. If the most
            # recent Saturday's week is still missing days, fall back rather than
            # headline a short week as if it were finished.
            wk = series[0].week_end
            dropped = [r for r in dropped if r.week_end < wk]
        inv = weekly_inventory(client, [r.week_end for r in series[:4]])
        skus = _annotate(sku_detail(client, wk), goals)
        mix = category_mix(client, wk)

        pspw_by_week: dict[date, float] = {}
        upspw_by_week: dict[date, float] = {}
        for r in series[:4]:
            wk_skus = skus if r.week_end == wk else _annotate(sku_detail(client, r.week_end), goals)
            pspw_by_week[r.week_end] = sum(s.pspw for s in wk_skus if s.in_assortment and s.pspw)
            upspw_by_week[r.week_end] = sum(s.upspw for s in wk_skus if s.in_assortment and s.upspw)

        return render_weekly(
            {
                "week_end": wk,
                "series": series,
                "dropped": dropped,
                "record_high": record_high(client, wk, bounds.first_daily),
                "inventory": inv,
                "skus": skus,
                "mix": mix,
                "goals": goals,
                "pspw_by_week": pspw_by_week,
                "upspw_by_week": upspw_by_week,
                "sales_through": sales_through,
                "inv_through": inv_through,
            }
        )

    w = wtd(client, sales_through)
    # SKU movers over the same partial-week window
    client_skus = _q(
        client,
        f"""
        WITH cur AS (
          SELECT dpci, ANY_VALUE(item_description) AS descr,
                 SUM(sale_amount) AS amt, SUM(sale_quantity) AS units
          FROM {SALES}
          WHERE is_current AND sales_date BETWEEN '{w["week_start"]}' AND '{sales_through}'
          GROUP BY dpci
        ),
        prev AS (
          SELECT dpci, SUM(sale_amount) AS amt
          FROM {SALES}
          WHERE is_current AND sales_date
            BETWEEN '{w["week_start"] - timedelta(days=7)}'
                AND '{sales_through - timedelta(days=7)}'
          GROUP BY dpci
        ),
        inv AS (
          SELECT dpci,
                 COUNT(DISTINCT location_id) AS doors,
                 SAFE_DIVIDE(SUM(instock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS wip,
                 SAFE_DIVIDE(SUM(out_of_stock_q), SUM(instock_q) + SUM(out_of_stock_q)) * 100 AS oos
          FROM {INV} WHERE is_current AND inventory_date = '{inv_through}'
          GROUP BY dpci
        )
        SELECT cur.dpci, cur.descr, cur.amt, cur.units, prev.amt AS prev_amt,
               inv.doors, inv.wip, inv.oos
        FROM cur LEFT JOIN prev USING (dpci) LEFT JOIN inv USING (dpci)
        ORDER BY cur.amt DESC
        """,
    )
    return render_pulse(
        {
            "wtd": w,
            "skus": _annotate(client_skus, goals),
            "sales_through": sales_through,
            "inv_through": inv_through,
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["weekly", "pulse"], default="weekly")
    ap.add_argument("--week-ending", help="YYYY-MM-DD (weekly mode); default: last complete week")
    ap.add_argument(
        "--through",
        help="YYYY-MM-DD (pulse mode): pretend the feed stops here, to backtest "
        "a Thursday run against a past week",
    )
    ap.add_argument("--json", action="store_true", help="emit {main, replies} as JSON")
    args = ap.parse_args()

    out = build(args.mode, args.week_ending, args.through)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out["main"])
        for r in out["replies"]:
            print("\n" + "─" * 70 + "\n[threaded reply]\n")
            print(r)


if __name__ == "__main__":
    main()
