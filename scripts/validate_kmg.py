#!/usr/bin/env python3
"""KMG POS-report tie-out — validates the BPD warehouse against the vendor's
source-of-truth POS report (KMG JunW1'26, week ending 2026-06-06).

Run ON THE MACHINE THAT HOSTS THE WAREHOUSE (read-only; never writes):

    uv run python scripts/validate_kmg.py
    uv run python scripts/validate_kmg.py --db /path/to/bpd.duckdb

Exit code 0 = all run checks passed (skips allowed), 1 = at least one FAIL.

Expected values were extracted from the KMG report shipped 2026-06-08
("POS Reports - JunW1'26": Biom_Target_POS_JunW126.xlsx + PDF summary).
The report's own sheets disagree with each other by ±1 unit in places, so
tolerances below are deliberately not zero.

What is checked:
  1. Weekly totals (units + $) for the 12 fiscal weeks ending 3/21..6/6
  2. Per-TCIN units for week ending 6/6 (25 SKUs)
  3. Per-TCIN sales $ for week ending 6/6 (via the report's DPCI->TCIN map)
  4. Channel-originated split for week ending 6/6 (skipped if the warehouse
     has no channel column on sales_weekly)
  5. Per-TCIN on-hand inventory at week ending 6/6 (skipped if
     inventory_weekly is missing/empty)

A MISSING week (e.g. March weeks predating the BPD subscription) is reported
as SKIP, not FAIL — only present-but-wrong data fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

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

# Column-name candidates (subset of column_roles; kept inline so this script
# has zero project imports and works against any warehouse copy).
DATE_CANDS = ["sales_date", "week_end_date", "fiscal_week_end_d", "sale_date"]
UNIT_CANDS = ["sale_quantity", "units_sold", "units", "qty", "sales_units"]
DOLLAR_CANDS = ["sale_amount", "sales_dollars", "sales_amt", "dollars", "revenue"]
CHANNEL_CANDS = ["channel_originated", "origination_channel", "reporting_channel"]
INV_DATE_CANDS = ["report_date_dim", "week_end_date", "fiscal_week_end_d", "business_d"]
ONHAND_CANDS = ["on_hand_units", "on_hand_qty", "inventory_quantity", "inv_units", "on_hand"]


class Tally:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def line(self, status: str, label: str, detail: str = "") -> None:
        mark = {"PASS": "+", "FAIL": "X", "SKIP": "~"}[status]
        print(f"  [{mark}] {status:4s} {label:46s} {detail}")
        if status == "PASS":
            self.passed += 1
        elif status == "FAIL":
            self.failed += 1
        else:
            self.skipped += 1


def cols_of(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def pick(cands: list[str], available: list[str]) -> str | None:
    avail = set(available)
    for c in cands:
        if c in avail:
            return c
    return None


def within(actual: float, expected: float, rel_tol: float, abs_floor: float = 0.0) -> bool:
    if expected == 0:
        return abs(actual) <= max(abs_floor, 1.0)
    return abs(actual - expected) <= max(abs(expected) * rel_tol, abs_floor)


def date_expr(conn: duckdb.DuckDBPyConnection, table: str, col: str) -> str:
    """VARCHAR-shipped dates need a cast (Target quirk — see README)."""
    (dtype,) = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name=? AND column_name=?",
        [table, col],
    ).fetchone()
    if "VARCHAR" in dtype.upper() or "TEXT" in dtype.upper():
        return f'CAST("{col}" AS DATE)'
    return f'"{col}"'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".bpd-mcp" / "bpd.duckdb"),
        help="Path to the BPD DuckDB warehouse (default: ~/.bpd-mcp/bpd.duckdb)",
    )
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: warehouse not found at {db}", file=sys.stderr)
        return 1

    conn = duckdb.connect(str(db), read_only=True)
    t = Tally()
    print(f"KMG JunW1'26 tie-out — warehouse: {db}")

    # ---- resolve sales_weekly shape -------------------------------------
    sw_cols = cols_of(conn, "sales_weekly")
    if not sw_cols:
        print("ERROR: sales_weekly table not found — run bpd_sync_new_files first.")
        return 1
    dcol = pick(DATE_CANDS, sw_cols)
    ucol = pick(UNIT_CANDS, sw_cols)
    scol = pick(DOLLAR_CANDS, sw_cols)
    if not (dcol and ucol):
        print(f"ERROR: can't resolve date/units columns in sales_weekly: {sw_cols}")
        return 1
    dexpr = date_expr(conn, "sales_weekly", dcol)
    print(f"sales_weekly columns: date={dcol} units={ucol} dollars={scol or 'N/A'}\n")

    # ---- check 1: 12 weekly totals --------------------------------------
    print("CHECK 1 — weekly totals (units / $), 12 fiscal weeks")
    actual = {
        str(r[0]): (r[1], r[2])
        for r in conn.execute(
            f"SELECT {dexpr} AS wk, SUM({ucol}), "
            f"{f'SUM({scol})' if scol else 'NULL'} "
            f"FROM sales_weekly GROUP BY wk"
        ).fetchall()
    }
    for wk, exp_u, exp_d in EXPECTED_WEEKS:
        if wk not in actual:
            t.line("SKIP", f"w/e {wk}", "no data in warehouse (pre-subscription?)")
            continue
        act_u, act_d = actual[wk]
        ok_u = within(act_u or 0, exp_u, TOL_WEEKLY)
        detail = f"units {act_u:,} vs {exp_u:,}"
        ok_d = True
        if scol and exp_d and act_d is not None:
            ok_d = within(act_d, exp_d, TOL_WEEKLY)
            detail += f" | $ {act_d:,.0f} vs {exp_d:,.0f}"
        t.line("PASS" if (ok_u and ok_d) else "FAIL", f"w/e {wk}", detail)

    # ---- check 2 + 3: per-TCIN units and dollars (w/e 6/6) --------------
    print(f"\nCHECK 2+3 — per-TCIN units and $ for w/e {WEEK}")
    rows = conn.execute(
        f"SELECT tcin, SUM({ucol}), {f'SUM({scol})' if scol else 'NULL'} "
        f"FROM sales_weekly WHERE {dexpr} = DATE '{WEEK}' GROUP BY tcin"
    ).fetchall()
    by_tcin = {int(r[0]): (r[1], r[2]) for r in rows if r[0] is not None}
    if not by_tcin:
        t.line("SKIP", f"per-TCIN w/e {WEEK}", "week not in warehouse")
    else:
        for tcin, (_dpci, model, exp_u, exp_d, _oh) in sorted(EXPECTED_TCIN.items()):
            if tcin not in by_tcin:
                if abs(exp_u) <= 1:  # near-zero SKUs may legitimately have no rows
                    t.line("SKIP", f"{tcin} {model}", f"no rows (expected ~{exp_u} units)")
                else:
                    t.line("FAIL", f"{tcin} {model}", f"MISSING — expected {exp_u:,} units")
                continue
            act_u, act_d = by_tcin[tcin]
            ok_u = within(act_u or 0, exp_u, TOL_SKU, TOL_ABS_SMALL / 10)
            detail = f"units {act_u:,} vs {exp_u:,}"
            ok_d = True
            if scol and exp_d is not None and act_d is not None:
                ok_d = within(act_d, exp_d, TOL_SKU, TOL_ABS_SMALL)
                detail += f" | $ {act_d:,.0f} vs {exp_d:,.0f}"
            t.line("PASS" if (ok_u and ok_d) else "FAIL", f"{tcin} {model}", detail)
        # The TCIN=0 SKU in the report can only be checked in aggregate.
        total_u = sum(u or 0 for u, _ in by_tcin.values())
        exp_total = sum(v[2] for v in EXPECTED_TCIN.values()) + UNMAPPED_DPCI_UNITS
        t.line(
            "PASS" if within(total_u, exp_total, TOL_WEEKLY) else "FAIL",
            "all-TCIN total (incl. unmapped 253-04-3809)",
            f"units {total_u:,} vs {exp_total:,}",
        )

    # ---- check 4: channel-originated split (w/e 6/6) --------------------
    print(f"\nCHECK 4 — channel-originated $ split for w/e {WEEK}")
    ccol = pick(CHANNEL_CANDS, sw_cols)
    if not (ccol and scol):
        t.line("SKIP", "channel split", f"no channel column on sales_weekly ({sw_cols})")
    else:
        crows = conn.execute(
            f'SELECT "{ccol}", SUM({scol}) FROM sales_weekly '
            f"WHERE {dexpr} = DATE '{WEEK}' GROUP BY 1"
        ).fetchall()
        if not crows:
            t.line("SKIP", "channel split", "week not in warehouse")
        else:
            print(f"     (channel column: {ccol})")
            store = sum(d or 0 for ch, d in crows if ch and "store" in str(ch).lower())
            online = sum(d or 0 for ch, d in crows if ch and "store" not in str(ch).lower())
            ok_s = within(store, EXPECTED_CHANNEL["store_originated_dollars"], TOL_SKU)
            ok_o = within(online, EXPECTED_CHANNEL["online_originated_dollars"], TOL_SKU)
            t.line(
                "PASS" if ok_s else "FAIL", "store-originated $",
                f"${store:,.0f} vs ${EXPECTED_CHANNEL['store_originated_dollars']:,.0f}",
            )
            t.line(
                "PASS" if ok_o else "FAIL", "online-originated $ (online+flex)",
                f"${online:,.0f} vs ${EXPECTED_CHANNEL['online_originated_dollars']:,.0f}",
            )

    # ---- check 5: per-TCIN on-hand inventory (w/e 6/6) ------------------
    print(f"\nCHECK 5 — per-TCIN on-hand units at w/e {WEEK}")
    inv_cols = cols_of(conn, "inventory_weekly")
    idcol = pick(INV_DATE_CANDS, inv_cols) if inv_cols else None
    ohcol = pick(ONHAND_CANDS, inv_cols) if inv_cols else None
    if not (idcol and ohcol):
        t.line("SKIP", "inventory", f"inventory_weekly missing usable columns ({inv_cols})")
    else:
        idexpr = date_expr(conn, "inventory_weekly", idcol)
        irows = conn.execute(
            f"SELECT tcin, SUM({ohcol}) FROM inventory_weekly "
            f"WHERE {idexpr} = DATE '{WEEK}' GROUP BY tcin"
        ).fetchall()
        inv = {int(r[0]): (r[1] or 0) for r in irows if r[0] is not None}
        if not inv:
            t.line("SKIP", "inventory", f"no inventory rows for w/e {WEEK}")
        else:
            for tcin, (_dpci, model, _u, _d, exp_oh) in sorted(EXPECTED_TCIN.items()):
                if exp_oh < 50:  # noise-level SKUs
                    continue
                if tcin not in inv:
                    t.line("FAIL", f"{tcin} {model}", f"MISSING — expected OH {exp_oh:,}")
                    continue
                act = inv[tcin]
                t.line(
                    "PASS" if within(act, exp_oh, TOL_INV) else "FAIL",
                    f"{tcin} {model}", f"OH {act:,.0f} vs {exp_oh:,}",
                )

    # ---- summary ---------------------------------------------------------
    print(f"\nRESULT: {t.passed} passed, {t.failed} failed, {t.skipped} skipped")
    if t.failed:
        print(
            "\nFailure triage:\n"
            "  - Whole weeks off by an exact factor (2.0x): duplicate loads — run\n"
            "    bpd_refresh_dataset(<dataset>, full=true) then re-run this script.\n"
            "  - Whole weeks slightly off (<2%): late POS adjustments in a newer\n"
            "    BPD file — re-sync and re-run.\n"
            "  - Single SKUs missing: check _file_ledger for failed files\n"
            "    (SELECT * FROM _file_ledger WHERE status='failed').\n"
            "  - All dollars off but units right (or vice versa): column mapping —\n"
            "    check the header printed above against the real table schema."
        )
    return 1 if t.failed else 0


if __name__ == "__main__":
    sys.exit(main())
