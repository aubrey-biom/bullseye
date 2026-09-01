#!/usr/bin/env python3
"""KMG POS-report tie-out — validates the BPD BigQuery data layer against the
vendor's source-of-truth POS report (KMG JunW1'26, week ending 2026-06-06).

Read-only; it opens the same `BigQueryWarehouse` the MCP server uses, so what it
measures is exactly what the tools would return:

    uv run python scripts/validate_kmg.py
    uv run python scripts/validate_kmg.py --project biom-reporting-s26 --location us-central1

Exit code 0 = all run checks passed (skips and documented KNOWN report-side
discrepancies allowed), 1 = at least one FAIL. `--strict` also fails on KNOWN.

Cost: four real queries over `sales_weekly` / `inventory_weekly` — 94 MB billed
on the 2026-09-01 run (each is dry-run first, and that upper-bound estimate is
printed). This is not a 0-byte script; it reads production on purpose.

Expected values were extracted from the KMG report shipped 2026-06-08
("POS Reports - JunW1'26": Biom_Target_POS_JunW126.xlsx + PDF summary).
The report's own sheets disagree with each other by ±1 unit in places, so
tolerances below are deliberately not zero.

What is checked:
  1. Weekly totals (units + $) for the 12 fiscal weeks ending 3/21..6/6
  2. Per-TCIN units for week ending 6/6 (25 SKUs)
  3. Per-TCIN sales $ for week ending 6/6 (via the report's DPCI->TCIN map)
  4. Channel-originated split for week ending 6/6 (skipped if the registry's
     sales_weekly projection has no channel column)
  5. Per-TCIN on-hand inventory at week ending 6/6 (skipped if
     inventory_weekly is missing/empty)

A MISSING week is reported as SKIP, not FAIL — only present-but-wrong data
fails.

WHY THIS SCRIPT EXISTS AFTER THE BIGQUERY SWAP
----------------------------------------------
It is the acceptance gate for the data-layer migration: the KMG report is
external to both the old DuckDB warehouse and the new BigQuery one, so
re-running it answers "did the swap move any number?" against a fixed ruler.

No column name is hardcoded. Column resolution goes through
`column_roles.resolve_column`, and date columns through
`ResolvedColumn.select_as_date()` — which emits SAFE_CAST, not CAST, because a
plain CAST over Target's `""` date placeholder returns a hard 400 and takes the
whole query with it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, ClassVar

# Importable without an editable install, so the script runs from a checkout.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bpd_mcp.bq import (  # noqa: E402
    BQ_LOCATION_DEFAULT,
    BQ_PROJECT_DEFAULT,
    BigQueryWarehouse,
    CredentialsUnavailable,
    QueryTooExpensive,
    quote_ident,
)
from bpd_mcp.column_roles import (  # noqa: E402
    ColumnNotFound,
    ResolvedColumn,
    resolve_column,
    table_exists,
)

# ---------------------------------------------------------------------------
# Expected values (KMG JunW1'26 report)
# ---------------------------------------------------------------------------

# (week_end_date, units, dollars) — "12 Week Comparison" sheet, TOTAL rows.
EXPECTED_WEEKS: list[tuple[str, int, float]] = [
    ("2026-03-21", 22493, 202546.0),
    ("2026-03-28", 22100, 194580.0),
    ("2026-04-04", 21352, 213179.0),
    ("2026-04-11", 22681, 206062.0),
    ("2026-04-18", 24154, 231556.0),
    ("2026-04-25", 26362, 257783.0),
    ("2026-05-02", 36855, 352196.0),
    ("2026-05-09", 38648, 331785.0),
    ("2026-05-16", 31417, 270684.0),
    ("2026-05-23", 38140, 331514.0),
    ("2026-05-30", 34052, 336867.0),
    ("2026-06-06", 32694, 334946.0),
]

# KNOWN PRE-EXISTING REPORT DISCREPANCIES — the report is wrong, not the data.
#
# `week -> (units the warehouse is PINNED to, why)`. A week listed here is
# reported as KNOWN rather than FAIL, but ONLY while the warehouse still
# returns the pinned figure: the pin is itself a tight assertion, so a real
# regression on that week fails as loudly as any other. `--strict` counts
# KNOWN as failure for anyone who wants a fully clean tie-out or nothing.
#
# w/e 2026-04-04: the report's TOTAL row says 21,352 units while its own dollar
# total ($213,179) ties to the warehouse to 0.002%. Verified 2026-09-01 — all
# three Target feeds agree with the warehouse and disagree with the report:
#     biom_canvas.fct_target_sales (data_grain='weekly')   23,994 u / $213,175.31
#     bpd_raw.weekly_sales_tcin_loc                        23,994 u / $213,175.31
#     bpd_raw.history_sales_weekly                         23,994 u / $213,175.31
# The last of those is the Kiteworks file the DuckDB warehouse loaded directly,
# so the pre-migration number was 23,994 too. Nothing about the swap moved it.
KNOWN_REPORT_DISCREPANCIES: dict[str, tuple[float, str]] = {
    "2026-04-04": (
        23994.0,
        "report units 21,352 disagree with all three Target feeds (canvas, raw "
        "weekly, raw history), which agree at 23,994, while the report's own $ "
        "total ties to 0.002% — a report-side unit error predating the swap",
    ),
}

# Per-TCIN expectations for week ending 2026-06-06.
# tcin -> (dpci, model, units, dollars, on_hand)
#   units    from "Channel Break-out by Item" (Total Units)
#   dollars  from "DPCI Detail" (LATEST WEEK TY; report rounds to $1)
#   on_hand  from "DPCI Level" (OH column; EOH only, excludes on-water)
# dollars=None where the report shows no per-DPCI dollar figure.
EXPECTED_TCIN: dict[int, tuple[str, str, int, float | None, int]] = {
    94928291: ("003-02-5627", "K-60WIP-DSN-COM-3PK", 3788, 45307.0, 74124),
    89854823: ("003-02-1327", "P-DIS-WHI", 1368, 25500.0, 19211),
    89854825: ("003-02-0228", "P-DIS-TAN", 1519, 27746.0, 11966),
    94928292: ("003-02-5042", "P-60WIP-DSN-CIT", 4128, 19704.0, 92566),
    94799734: ("007-07-0096", "K-DIS-2BAB-WHI", 895, 24951.0, 13134),
    94928290: ("003-02-2154", "P-DIS-BRU", 786, 18509.0, 11879),
    94799739: ("007-07-9942", "K-60WIP-BAB-FRA-4PK", 1365, 18353.0, 29302),
    89854821: ("003-02-1080", "P-DIS-EUC", 1342, 24496.0, 14372),
    94928293: ("003-02-5488", "K-60WIP-AP-COM", 1452, 17615.0, 16814),
    94799740: ("007-07-8361", "K-60WIP-BAB-FRA-12PK", 566, 17959.0, 15554),
    94799736: ("007-07-1897", "K-DIS-2BAB-LGR", 456, 12948.0, 14078),
    94799737: ("007-07-3543", "P-60WIP-BAB-FRA", 3435, 10039.0, 53354),
    94928289: ("003-02-8568", "P-60WIP-DSN-ALP", 2201, 10486.0, 97930),
    93197979: ("003-02-4440", "P-60WIP-AP-FRA", 2325, 11058.0, 17977),
    89854826: ("003-02-3616", "P-60WIP-AP-STL", 2362, 11211.0, 25496),
    94799738: ("007-07-5306", "K-DIS-2BAB-PUR", 358, 10019.0, 12003),
    93197977: ("003-02-6532", "P-60WIP-AP-NER", 917, 4395.0, 23189),
    94979718: ("253-04-0088", "P-40WIP-6IN-SAN-STL", 1547, 5846.0, 11865),
    94979715: ("253-04-0086", "P-30WIP-BOD-NAT-TRV", 608, 1707.0, 5211),
    94979716: ("253-04-9259", "P-20WIP-SAN-STL-TRV", 65, 138.0, 921),
    94723688: ("003-02-9612", "P-DIS-TER", 8, 126.0, 306),
    94643458: ("003-02-0381", "P-DIS-DGR", 4, 63.0, 17),
    94643459: ("003-02-7872", "P-DIS-BLK", -1, -19.0, 18),
    94723687: ("003-02-0736", "P-6DIS-WHI", -1, -10.0, 2),
    94643460: ("003-02-6264", "P-DIS-LGR", 26, 445.0, 30),
}
# NOTE: DPCI 253-04-3809 (P-6DIS-1SAN-WHI-STL, 1,174 units / $16,331 / OH
# 14,915) is listed with TCIN=0 in the KMG report, so it can't be matched by
# TCIN here. It still contributes to the weekly totals in check 1.
UNMAPPED_DPCI_UNITS = 1174
UNMAPPED_DPCI_DOLLARS = 16331.0

# Channel splits for week ending 2026-06-06 ("Summary by Channel" sheet).
# Store-originated vs online-originated (online + flex/ship-from-store).
EXPECTED_CHANNEL = {
    "store_originated_dollars": 236359.92,
    "online_originated_dollars": 8573.33 + 89970.62,  # online + flex
}

WEEK = "2026-06-06"

TOL_WEEKLY = 0.005      # 0.5% — report sheets self-disagree by ±1 unit
TOL_SKU = 0.01          # 1% per-SKU
TOL_ABS_SMALL = 50.0    # absolute floor for tiny/negative SKU values
TOL_INV = 0.02          # 2% — snapshot timing differences vs report cut
# Channel attribution drifts between BPD and KMG, and it is offsetting: on the
# 2026-09-01 run store-originated read $1,159 (0.49%) LOW and online-originated
# $1,202 (1.22%) HIGH, while the combined total matched to 0.013%. So the split
# gets a looser tolerance and the total-reconciliation line below stays tight —
# a real attribution regression would move the total, not just shuffle it.
TOL_CHANNEL = 0.02

SALES = "sales_weekly"
INVENTORY = "inventory_weekly"

# Roles that COLUMN_ROLES does not define (no tool consumes them), supplied
# here as extra candidates so resolution still goes through one code path.
CHANNEL_CANDS = ("channel_originated", "origination_channel", "reporting_channel")
# KMG's "OH" column includes in-transit units for SOME SKUs and not others.
# Measured 2026-09-01: 12 of the 21 checked SKUs match `ending_on_hand_q`
# alone, and the other 9 match only `ending_on_hand_q + ending_on_transfer_q`
# (exactly, in every case — this is a definition difference, not drift). The
# check therefore accepts EITHER definition and prints which one matched, so
# the per-SKU convention stays visible rather than being averaged away.
ONTRANSFER_CANDS = ("ending_on_transfer_q", "on_transfer_q", "in_transit_q")


class Tally:
    """Counters plus the emitted lines, so tests can assert on specific checks.

    Four outcomes, not three. KNOWN is a documented report-side error whose
    warehouse-side value is pinned in `KNOWN_REPORT_DISCREPANCIES` — it does not
    fail the run by default (an acceptance gate that can never go green stops
    being read), but it is never silent and it is never unchecked.
    """

    STATUSES: ClassVar[tuple[str, ...]] = ("PASS", "KNOWN", "FAIL", "SKIP")
    _MARKS: ClassVar[dict[str, str]] = {"PASS": "+", "KNOWN": "!", "FAIL": "X", "SKIP": "~"}

    def __init__(self) -> None:
        self.passed = 0
        self.known = 0
        self.failed = 0
        self.skipped = 0
        self.lines: list[tuple[str, str, str]] = []

    def line(self, status: str, label: str, detail: str = "") -> None:
        mark = self._MARKS[status]
        print(f"  [{mark}] {status:5s} {label:46s} {detail}")
        self.lines.append((status, label, detail))
        if status == "PASS":
            self.passed += 1
        elif status == "KNOWN":
            self.known += 1
        elif status == "FAIL":
            self.failed += 1
        else:
            self.skipped += 1

    def status_of(self, label_prefix: str) -> str | None:
        for status, label, _detail in self.lines:
            if label.startswith(label_prefix):
                return status
        return None


def cols_of(warehouse: Any, table: str) -> list[str]:
    """Column names of a logical table — from the registry's own projection.

    The DuckDB version read `information_schema.columns WHERE
    table_schema='main'`. That has no BigQuery equivalent for a logical table:
    a CTE-injected name appears in no catalogue at all, and INFORMATION_SCHEMA
    bills a 10 MB minimum besides. `logical_schema` derives the answer from a
    cached 0-byte dry run of the table body.
    """
    if not table_exists(warehouse, table):
        return []
    return [name for name, _dtype in warehouse.logical_schema(table)]


def resolve(
    warehouse: Any,
    table: str,
    role: str,
    extra: tuple[str, ...] = (),
) -> ResolvedColumn | None:
    """`resolve_column`, but None instead of raising — every caller here degrades
    to SKIP rather than dying on an absent optional column."""
    try:
        return resolve_column(warehouse, table, role, extra_candidates=extra)
    except ColumnNotFound:
        return None


def within(actual: float, expected: float, rel_tol: float, abs_floor: float = 0.0) -> bool:
    if expected == 0:
        return abs(actual) <= max(abs_floor, 1.0)
    return abs(actual - expected) <= max(abs(expected) * rel_tol, abs_floor)


def date_expr(col: ResolvedColumn) -> str:
    """SQL that yields `col` as a DATE.

    Delegates to `ResolvedColumn.select_as_date`, which emits the column bare
    when it is already DATE/TIMESTAMP and `SAFE_CAST(... AS DATE)` when Target
    ships it as a STRING. SAFE_CAST is mandatory, not defensive: a plain CAST
    over one `""` placeholder row fails the entire query with 400 Invalid date.
    """
    return col.select_as_date()


def _sum(col: ResolvedColumn | None) -> str:
    return f"SUM({quote_ident(col.name)})" if col is not None else "NULL"


class _Runner:
    """Executes statements through the warehouse, dry-running each one first so
    the tie-out can report what it cost."""

    def __init__(self, warehouse: Any) -> None:
        self.warehouse = warehouse
        self.bytes_estimated = 0

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            job = self.warehouse.dry_run(sql)
            self.bytes_estimated += int(job.total_bytes_processed or 0)
        except Exception:
            # A dry run is a nicety (cost reporting). Never let it stop the run.
            pass
        _cols, rows = self.warehouse.execute_sql(sql)
        return rows


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_weekly_totals(run: _Runner, t: Tally, cols: dict[str, ResolvedColumn | None]) -> None:
    print("CHECK 1 — weekly totals (units / $), 12 fiscal weeks")
    dexpr = date_expr(cols["date"])
    weeks_sql = ", ".join(f"DATE '{wk}'" for wk, _u, _d in EXPECTED_WEEKS)
    rows = run.query(
        f"SELECT {dexpr} AS wk, {_sum(cols['units'])}, {_sum(cols['dollars'])} "
        f"FROM {SALES} WHERE {dexpr} IN ({weeks_sql}) GROUP BY wk"
    )
    actual = {str(r[0]): (r[1], r[2]) for r in rows if r[0] is not None}
    for wk, exp_u, exp_d in EXPECTED_WEEKS:
        if wk not in actual:
            t.line("SKIP", f"w/e {wk}", "no data in warehouse (pre-subscription?)")
            continue
        act_u, act_d = actual[wk]
        ok_u = within(act_u or 0, exp_u, TOL_WEEKLY)
        detail = f"units {act_u or 0:,.0f} vs {exp_u:,}"
        ok_d = True
        if cols["dollars"] is not None and exp_d and act_d is not None:
            ok_d = within(act_d, exp_d, TOL_WEEKLY)
            detail += f" | $ {act_d:,.0f} vs {exp_d:,.0f}"
        pinned = KNOWN_REPORT_DISCREPANCIES.get(wk)
        if not ok_u and ok_d and pinned is not None and within(act_u or 0, pinned[0], TOL_WEEKLY):
            # Report-side error, warehouse still on its pinned pre-migration
            # value. Off the pin by more than TOL_WEEKLY and this falls through
            # to a plain FAIL.
            t.line(
                "KNOWN", f"w/e {wk}",
                f"{detail}  <- report-side error; warehouse pinned at {pinned[0]:,.0f}",
            )
            continue
        t.line("PASS" if (ok_u and ok_d) else "FAIL", f"w/e {wk}", detail)


def check_per_tcin(run: _Runner, t: Tally, cols: dict[str, ResolvedColumn | None]) -> None:
    print(f"\nCHECK 2+3 — per-TCIN units and $ for w/e {WEEK}")
    dexpr = date_expr(cols["date"])
    tcin = quote_ident(cols["tcin"].name)
    rows = run.query(
        f"SELECT {tcin}, {_sum(cols['units'])}, {_sum(cols['dollars'])} "
        f"FROM {SALES} WHERE {dexpr} = DATE '{WEEK}' GROUP BY {tcin}"
    )
    by_tcin = {int(r[0]): (r[1], r[2]) for r in rows if r[0] is not None}
    if not by_tcin:
        t.line("SKIP", f"per-TCIN w/e {WEEK}", "week not in warehouse")
        return
    for tc, (_dpci, model, exp_u, exp_d, _oh) in sorted(EXPECTED_TCIN.items()):
        if tc not in by_tcin:
            if abs(exp_u) <= 1:  # near-zero SKUs may legitimately have no rows
                t.line("SKIP", f"{tc} {model}", f"no rows (expected ~{exp_u} units)")
            else:
                t.line("FAIL", f"{tc} {model}", f"MISSING — expected {exp_u:,} units")
            continue
        act_u, act_d = by_tcin[tc]
        ok_u = within(act_u or 0, exp_u, TOL_SKU, TOL_ABS_SMALL / 10)
        detail = f"units {act_u or 0:,.0f} vs {exp_u:,}"
        ok_d = True
        if cols["dollars"] is not None and exp_d is not None and act_d is not None:
            ok_d = within(act_d, exp_d, TOL_SKU, TOL_ABS_SMALL)
            detail += f" | $ {act_d:,.0f} vs {exp_d:,.0f}"
        t.line("PASS" if (ok_u and ok_d) else "FAIL", f"{tc} {model}", detail)
    # The TCIN=0 SKU in the report can only be checked in aggregate.
    total_u = sum(u or 0 for u, _ in by_tcin.values())
    exp_total = sum(v[2] for v in EXPECTED_TCIN.values()) + UNMAPPED_DPCI_UNITS
    t.line(
        "PASS" if within(total_u, exp_total, TOL_WEEKLY) else "FAIL",
        "all-TCIN total (incl. unmapped 253-04-3809)",
        f"units {total_u:,.0f} vs {exp_total:,}",
    )


def check_channel_split(run: _Runner, t: Tally, cols: dict[str, ResolvedColumn | None]) -> None:
    print(f"\nCHECK 4 — channel-originated $ split for w/e {WEEK}")
    ccol, scol = cols["channel"], cols["dollars"]
    if ccol is None or scol is None:
        t.line("SKIP", "channel split", f"no channel column on {SALES} ({cols_of(run.warehouse, SALES)})")
        return
    dexpr = date_expr(cols["date"])
    ch = quote_ident(ccol.name)
    rows = run.query(
        f"SELECT {ch}, {_sum(scol)} FROM {SALES} "
        f"WHERE {dexpr} = DATE '{WEEK}' GROUP BY {ch}"
    )
    if not rows:
        t.line("SKIP", "channel split", "week not in warehouse")
        return
    print(f"     (channel column: {ccol.name})")
    store = sum(d or 0 for c, d in rows if c and "store" in str(c).lower())
    online = sum(d or 0 for c, d in rows if c and "store" not in str(c).lower())
    ok_s = within(store, EXPECTED_CHANNEL["store_originated_dollars"], TOL_CHANNEL)
    ok_o = within(online, EXPECTED_CHANNEL["online_originated_dollars"], TOL_CHANNEL)
    t.line(
        "PASS" if ok_s else "FAIL", "store-originated $",
        f"${store:,.0f} vs ${EXPECTED_CHANNEL['store_originated_dollars']:,.0f}",
    )
    t.line(
        "PASS" if ok_o else "FAIL", "online-originated $ (online+flex)",
        f"${online:,.0f} vs ${EXPECTED_CHANNEL['online_originated_dollars']:,.0f}",
    )
    # The stronger invariant: the two splits must sum to the report total
    # tightly, even when attribution drifts between them.
    exp_total = sum(EXPECTED_CHANNEL.values())
    t.line(
        "PASS" if within(store + online, exp_total, TOL_WEEKLY) else "FAIL",
        "channel total (store + online)",
        f"${store + online:,.0f} vs ${exp_total:,.0f}",
    )


def check_inventory(run: _Runner, t: Tally) -> None:
    print(f"\nCHECK 5 — per-TCIN on-hand units at w/e {WEEK}")
    w = run.warehouse
    inv_cols = cols_of(w, INVENTORY)
    idcol = resolve(w, INVENTORY, "date")
    ohcol = resolve(w, INVENTORY, "on_hand")
    tccol = resolve(w, INVENTORY, "tcin")
    otcol = resolve(w, INVENTORY, "on_transfer", ONTRANSFER_CANDS)
    if idcol is None or ohcol is None or tccol is None:
        t.line("SKIP", "inventory", f"{INVENTORY} missing usable columns ({inv_cols})")
        return
    idexpr = date_expr(idcol)
    tcin = quote_ident(tccol.name)
    rows = run.query(
        f"SELECT {tcin}, {_sum(ohcol)}, {_sum(otcol)} FROM {INVENTORY} "
        f"WHERE {idexpr} = DATE '{WEEK}' GROUP BY {tcin}"
    )
    inv = {int(r[0]): ((r[1] or 0), (r[2] or 0)) for r in rows if r[0] is not None}
    if not inv:
        t.line("SKIP", "inventory", f"no inventory rows for w/e {WEEK}")
        return
    if otcol is not None:
        print(f"     (on-hand: {ohcol.name}; on-transfer: {otcol.name} — either definition may match)")
    for tc, (_dpci, model, _u, _d, exp_oh) in sorted(EXPECTED_TCIN.items()):
        if exp_oh < 50:  # noise-level SKUs
            continue
        if tc not in inv:
            t.line("FAIL", f"{tc} {model}", f"MISSING — expected OH {exp_oh:,}")
            continue
        oh, ot = inv[tc]
        # KMG's OH definition varies by SKU (see ONTRANSFER_CANDS comment):
        # accept on-hand alone OR on-hand + on-transfer.
        ok_oh = within(oh, exp_oh, TOL_INV)
        ok_sum = otcol is not None and within(oh + ot, exp_oh, TOL_INV)
        if ok_oh:
            detail = f"OH {oh:,.0f} vs {exp_oh:,}"
        elif ok_sum:
            detail = f"OH+transfer {oh + ot:,.0f} (OH {oh:,.0f}) vs {exp_oh:,}"
        else:
            detail = (
                f"OH {oh:,.0f} / OH+transfer {oh + ot:,.0f} vs {exp_oh:,}"
                if otcol is not None
                else f"OH {oh:,.0f} vs {exp_oh:,}"
            )
        t.line("PASS" if (ok_oh or ok_sum) else "FAIL", f"{tc} {model}", detail)


def run_checks(warehouse: Any) -> Tally:
    """All five checks against an already-constructed warehouse.

    Split out of `main` so tests can drive it with a registry of literal-row
    fixture tables (0 bytes billed) instead of production.
    """
    t = Tally()
    run = _Runner(warehouse)

    if not table_exists(warehouse, SALES):
        print(f"ERROR: {SALES} is not a logical table in this registry.")
        t.line("FAIL", SALES, "absent from the registry")
        return t

    cols: dict[str, ResolvedColumn | None] = {
        "date": resolve(warehouse, SALES, "date"),
        "units": resolve(warehouse, SALES, "units"),
        "dollars": resolve(warehouse, SALES, "dollars"),
        "tcin": resolve(warehouse, SALES, "tcin"),
        "channel": resolve(warehouse, SALES, "channel", CHANNEL_CANDS),
    }
    if cols["date"] is None or cols["units"] is None or cols["tcin"] is None:
        print(
            f"ERROR: can't resolve date/units/tcin columns in {SALES}: "
            f"{cols_of(warehouse, SALES)}"
        )
        t.line("FAIL", SALES, "date/units/tcin unresolvable")
        return t
    print(
        f"{SALES} columns: date={cols['date'].name} units={cols['units'].name} "
        f"dollars={cols['dollars'].name if cols['dollars'] else 'N/A'} "
        f"channel={cols['channel'].name if cols['channel'] else 'N/A'}\n"
    )

    check_weekly_totals(run, t, cols)
    check_per_tcin(run, t, cols)
    check_channel_split(run, t, cols)
    check_inventory(run, t)

    print(
        f"\nRESULT: {t.passed} passed, {t.known} known-discrepancy, "
        f"{t.failed} failed, {t.skipped} skipped"
    )
    if run.bytes_estimated:
        # The dry-run figure is an UPPER BOUND, not the bill. The weekly tables
        # union canvas with raw on `WHERE date > (SELECT MAX(...))`, which the
        # planner cannot prune statically but the executor prunes at runtime —
        # measured 313 MB estimated vs 94 MB actually billed on 2026-09-01.
        print(
            f"        (dry-run upper bound {run.bytes_estimated / 1e6:,.1f} MB; "
            f"runtime pruning bills less)"
        )
    return t


TRIAGE = """
Failure triage:
  - Whole weeks off by an exact factor (2.0x): the registry is double-counting a
    source. sales_weekly unions canvas with bpd_raw.weekly_sales_tcin_loc on a
    computed MAX() boundary; a third branch over bpd_raw.history_sales_weekly
    would overlap 2026-04-04..2026-05-02. Check bq.LOGICAL_TABLES['sales_weekly'].
  - Whole weeks slightly off (<2%): late POS adjustments landed upstream after
    the KMG report was cut. Compare the same week in canvas and in bpd_raw.
  - A week that used to report KNOWN now reports FAIL: the warehouse has moved
    OFF its pinned pre-migration value. That is a real regression — the pin in
    KNOWN_REPORT_DISCREPANCIES exists precisely to catch it.
  - Single SKUs missing: the upstream Kiteworks -> GCS -> BigQuery pipeline did
    not land a file. `bpd_data_freshness` shows the per-pattern file ledger.
  - All dollars off but units right (or vice versa): column mapping. Check the
    resolved column names printed above against COLUMN_ROLES.
  - Inventory consistently below KMG OH even with +transfer: KMG's cut may
    include on-order units — compare against ending_on_purchase_q before
    treating it as a data bug.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="KMG POS-report tie-out against the BPD BigQuery data layer."
    )
    ap.add_argument(
        "--project",
        default=BQ_PROJECT_DEFAULT,
        help=f"GCP project holding the BPD datasets (default: {BQ_PROJECT_DEFAULT})",
    )
    ap.add_argument(
        "--location",
        default=BQ_LOCATION_DEFAULT,
        help=f"BigQuery location (default: {BQ_LOCATION_DEFAULT})",
    )
    ap.add_argument(
        "--credentials",
        default=None,
        type=Path,
        help="Service-account JSON. Defaults to GCP_SA_KEY_B64 / "
        "GOOGLE_APPLICATION_CREDENTIALS / ADC.",
    )
    ap.add_argument(
        "--max-bytes-billed",
        default=None,
        type=int,
        help="Hard cap per query, in bytes. Unset = no cap (the run bills ~95 MB).",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on documented report-side discrepancies too "
        "(KNOWN_REPORT_DISCREPANCIES), not only on real failures.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        warehouse = BigQueryWarehouse(
            project=args.project,
            location=args.location,
            credentials_path=args.credentials,
            maximum_bytes_billed=args.max_bytes_billed,
        )
    except CredentialsUnavailable as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"KMG JunW1'26 tie-out — warehouse: {warehouse.db_path}")
    print(f"credential: {warehouse.credentials_source}\n")
    try:
        t = run_checks(warehouse)
    except QueryTooExpensive as e:
        print(f"ERROR: query exceeded --max-bytes-billed: {e}", file=sys.stderr)
        return 1
    finally:
        warehouse.close()

    failures = t.failed + (t.known if args.strict else 0)
    if failures:
        print(TRIAGE)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
