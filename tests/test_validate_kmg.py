"""Tests for scripts/validate_kmg.py — the KMG POS-report tie-out harness.

The harness is the acceptance gate for the BigQuery migration ("did the swap
move any number?"), so it gets real coverage rather than a smoke import.

Two tiers are used, matching the suite's split:

* default — pure python. Argument parsing (the README's own invocation must
  work), the tolerance function, the four-state tally, the SAFE_CAST date
  expression, and the internal consistency of the embedded KMG expectations.
* ``-m bq`` — the whole tie-out driven end to end against real BigQuery with
  every logical table replaced by literal fixture rows, so the SQL is genuinely
  executed by the real engine at 0 bytes billed. Seed rows that match the KMG
  report and the run must come back clean; break one number and exactly the
  expected checks must go red.
* ``-m bq_live`` — one subprocess run against production, which is the gate
  itself.

There is no DuckDB seeding any more (the pre-migration version wrote a
.duckdb file and ran the script over it): there is no file, and DuckDB cannot
parse the SQL this script now emits.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate_kmg.py"

from scripts import validate_kmg as vk  # noqa: E402  (conftest puts ROOT on sys.path)

STORE_FRAC = vk.EXPECTED_CHANNEL["store_originated_dollars"] / sum(
    vk.EXPECTED_CHANNEL.values()
)


# ---------------------------------------------------------------------------
# Fixture-row construction (shared by the -m bq tests)
# ---------------------------------------------------------------------------


def _sales_row(
    date: str, tcin: int, channel: str | None, units: float, dollars: float
) -> dict[str, Any]:
    row: dict[str, Any] = {"sales_date": date, "tcin": tcin, "location_id": 1111}
    if channel is not None:
        row["origination_channel"] = channel
    row["sale_quantity"] = float(units)
    row["sale_amount"] = float(dollars)
    return row


def _sales_rows(
    *,
    halve_tcin: int | None = None,
    week_unit_overrides: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Sales rows that reproduce the KMG report exactly.

    The latest week is split STORE/ONLINE in the report's own dollar proportion
    so check 4 has something real to reconcile; every earlier week is one
    aggregate row under a placeholder TCIN, which is all check 1 reads.
    """
    rows: list[dict[str, Any]] = []
    for tcin, (_dpci, _model, units, dollars, _oh) in vk.EXPECTED_TCIN.items():
        u = units // 2 if halve_tcin == tcin else units
        d = dollars if dollars is not None else 0.0
        u_store = round(u * STORE_FRAC)
        d_store = round(d * STORE_FRAC, 2)
        rows.append(_sales_row(vk.WEEK, tcin, "STORE", u_store, d_store))
        rows.append(_sales_row(vk.WEEK, tcin, "ONLINE", u - u_store, round(d - d_store, 2)))
    # The report's TCIN=0 SKU (DPCI 253-04-3809) — seeded under a placeholder
    # TCIN so the weekly and all-TCIN totals reconcile.
    u_store = round(vk.UNMAPPED_DPCI_UNITS * STORE_FRAC)
    d_store = round(vk.UNMAPPED_DPCI_DOLLARS * STORE_FRAC, 2)
    rows.append(_sales_row(vk.WEEK, 77777777, "STORE", u_store, d_store))
    rows.append(
        _sales_row(
            vk.WEEK,
            77777777,
            "ONLINE",
            vk.UNMAPPED_DPCI_UNITS - u_store,
            round(vk.UNMAPPED_DPCI_DOLLARS - d_store, 2),
        )
    )
    for wk, units, dollars in vk.EXPECTED_WEEKS:
        if wk == vk.WEEK:
            continue
        u = (week_unit_overrides or {}).get(wk, units)
        rows.append(_sales_row(wk, 88888888, "STORE", u, dollars))
    return rows


def _inventory_rows() -> list[dict[str, Any]]:
    return [
        {
            "business_d": vk.WEEK,
            "tcin": tcin,
            "location_id": 1111,
            "ending_on_hand_q": oh,
            "ending_on_transfer_q": 0,
        }
        for tcin, (_dpci, _model, _u, _d, oh) in vk.EXPECTED_TCIN.items()
    ]


# Every expectation the harness checks, counted once, so a test can assert the
# whole gate ran rather than "nothing failed because nothing executed".
EXPECTED_CHECK_COUNT = (
    len(vk.EXPECTED_WEEKS)                                          # check 1
    + len(vk.EXPECTED_TCIN) + 1                                     # checks 2+3
    + 3                                                             # check 4
    + sum(1 for v in vk.EXPECTED_TCIN.values() if v[4] >= 50)       # check 5
)


# ---------------------------------------------------------------------------
# Default tier — pure python
# ---------------------------------------------------------------------------


def test_readme_invocation_is_accepted() -> None:
    """The exact command README prints must parse.

    This is the regression the port exists to fix: the DuckDB-era parser had
    only `--db`, so the documented `--project ... --location ...` invocation
    exited 2 before touching any data.
    """
    args = vk.build_parser().parse_args(
        ["--project", "biom-reporting-s26", "--location", "us-central1"]
    )
    assert args.project == "biom-reporting-s26"
    assert args.location == "us-central1"
    assert args.strict is False

    readme = (ROOT / "README.md").read_text()
    printed = [
        line.strip()
        for line in readme.splitlines()
        if "scripts/validate_kmg.py" in line and line.strip().startswith(("uv run", "python"))
    ]
    assert printed, "README no longer shows how to invoke the tie-out"
    for line in printed:
        flags = line.split("validate_kmg.py", 1)[1].split()
        vk.build_parser().parse_args(flags)  # raises SystemExit(2) if README lies


def test_defaults_come_from_the_data_layer() -> None:
    """No second copy of the project/location constants to drift out of sync."""
    from bpd_mcp.bq import BQ_LOCATION_DEFAULT, BQ_PROJECT_DEFAULT

    args = vk.build_parser().parse_args([])
    assert args.project == BQ_PROJECT_DEFAULT
    assert args.location == BQ_LOCATION_DEFAULT
    assert args.credentials is None
    assert args.max_bytes_billed is None


def test_harness_imports_nothing_that_was_deleted() -> None:
    """`duckdb` is not an installed dependency — importing it means the script
    cannot even start, which is exactly how the port was discovered.

    Checked on the parsed AST, not on the text: the module docstring discusses
    the DuckDB era on purpose, and prose is not an import.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    gone = {"duckdb", "polars", "httpx", "pyarrow", "pandas", "tenacity"}
    assert not (imported & gone), f"imports a removed dependency: {sorted(imported & gone)}"
    assert "bpd_mcp" in imported, "the harness must exercise the real data layer"

    source = SCRIPT.read_text()
    assert "--db" not in source, "the DuckDB-era --db flag points at a file that no longer exists"
    # Column discovery goes through the registry's own projection, not a
    # catalogue query: BigQuery scopes INFORMATION_SCHEMA per dataset, so a
    # CTE-injected logical table appears in no catalogue at all — the DuckDB
    # query `FROM information_schema.columns WHERE table_schema='main'`
    # resolved to a *dataset* named information_schema and 404'd.
    assert "logical_schema" in source


def test_within_tolerance_semantics() -> None:
    assert vk.within(100.0, 100.0, 0.01)
    assert vk.within(100.9, 100.0, 0.01)
    assert not vk.within(101.1, 100.0, 0.01)
    # The absolute floor rescues tiny expectations from meaningless relative math.
    assert not vk.within(6.0, 1.0, 0.01)
    assert vk.within(6.0, 1.0, 0.01, abs_floor=10.0)
    # expected == 0 has no relative tolerance to speak of; ±1 unit is allowed.
    assert vk.within(1.0, 0.0, 0.5)
    assert not vk.within(2.0, 0.0, 0.5)
    assert vk.within(2.0, 0.0, 0.5, abs_floor=5.0)
    # Negative expectations (two SKUs in the report are returns) still work.
    assert vk.within(-19.5, -19.0, 0.05)
    assert not vk.within(-25.0, -19.0, 0.05)


def test_tally_separates_known_from_failed() -> None:
    t = vk.Tally()
    t.line("PASS", "a")
    t.line("KNOWN", "w/e 2026-04-04", "pinned")
    t.line("FAIL", "b")
    t.line("SKIP", "c")
    assert (t.passed, t.known, t.failed, t.skipped) == (1, 1, 1, 1)
    assert t.status_of("w/e 2026-04-04") == "KNOWN"
    assert t.status_of("nothing-like-this") is None
    assert len(t.lines) == 4


def test_date_expr_uses_safe_cast_for_string_dates() -> None:
    """A plain CAST over Target's `""` placeholder is a hard 400 on the whole query."""
    from bpd_mcp.column_roles import ResolvedColumn

    assert vk.date_expr(ResolvedColumn(name="sales_date", sql_type="DATE")) == "`sales_date`"
    assert (
        vk.date_expr(ResolvedColumn(name="business_d", sql_type="STRING"))
        == "SAFE_CAST(`business_d` AS DATE)"
    )
    expr = vk.date_expr(ResolvedColumn(name="week_end_date", sql_type="STRING"))
    assert "SAFE_CAST" in expr and "CAST(" in expr
    assert '"' not in expr, "identifiers must be backticked, not double-quoted"


def test_sum_helper_degrades_to_null() -> None:
    from bpd_mcp.column_roles import ResolvedColumn

    assert vk._sum(ResolvedColumn(name="sale_amount", sql_type="FLOAT64")) == "SUM(`sale_amount`)"
    assert vk._sum(None) == "NULL"


def test_expectations_are_internally_consistent() -> None:
    """The per-SKU sheet and the weekly-totals sheet of the report must agree.

    Not a test of the warehouse — a test that the numbers transcribed into this
    script are the report's, since every other assertion is measured against
    them.
    """
    weekly = {wk: (u, d) for wk, u, d in vk.EXPECTED_WEEKS}
    exp_u, exp_d = weekly[vk.WEEK]

    sku_u = sum(v[2] for v in vk.EXPECTED_TCIN.values()) + vk.UNMAPPED_DPCI_UNITS
    sku_d = sum(v[3] or 0.0 for v in vk.EXPECTED_TCIN.values()) + vk.UNMAPPED_DPCI_DOLLARS
    assert vk.within(sku_u, exp_u, vk.TOL_WEEKLY), (sku_u, exp_u)
    assert vk.within(sku_d, exp_d, vk.TOL_WEEKLY), (sku_d, exp_d)

    channel_total = sum(vk.EXPECTED_CHANNEL.values())
    assert vk.within(channel_total, exp_d, vk.TOL_WEEKLY), (channel_total, exp_d)

    assert len(vk.EXPECTED_TCIN) == 25
    assert len(vk.EXPECTED_WEEKS) == 12
    assert vk.EXPECTED_WEEKS[-1][0] == vk.WEEK


def test_known_discrepancies_are_pinned_and_real() -> None:
    """A KNOWN entry must disagree with the report and pin a specific value.

    An entry that agreed with the report would silently downgrade a passing
    week; an unpinned entry would excuse ANY number for that week, which is
    exactly the regression hole this harness exists to close.
    """
    weekly = {wk: u for wk, u, _d in vk.EXPECTED_WEEKS}
    assert vk.KNOWN_REPORT_DISCREPANCIES, "the 2026-04-04 discrepancy is documented behaviour"
    for wk, (pinned, why) in vk.KNOWN_REPORT_DISCREPANCIES.items():
        assert wk in weekly, f"{wk} is not one of the checked weeks"
        assert not vk.within(pinned, weekly[wk], vk.TOL_WEEKLY), (
            f"{wk} pins {pinned}, which the report's {weekly[wk]} already accepts — "
            "the entry is dead and should be deleted"
        )
        assert len(why) > 40, "a KNOWN entry has to explain itself"
    assert vk.KNOWN_REPORT_DISCREPANCIES["2026-04-04"][0] == 23994.0


# ---------------------------------------------------------------------------
# -m bq — the real tie-out logic, real BigQuery, literal fixture rows, 0 bytes
# ---------------------------------------------------------------------------


@pytest.mark.bq
def test_tie_out_is_clean_on_report_matching_data(fixture_warehouse: Any) -> None:
    w = fixture_warehouse(sales_weekly=_sales_rows(), inventory_weekly=_inventory_rows())
    t = vk.run_checks(w)
    assert (t.failed, t.known, t.skipped) == (0, 0, 0)
    assert t.passed == EXPECTED_CHECK_COUNT == 62
    # Spot-check that real checks ran, not just that nothing failed.
    assert t.status_of("w/e 2026-06-06") == "PASS"
    assert t.status_of("94928291 K-60WIP-DSN-COM-3PK") == "PASS"
    assert t.status_of("store-originated $") == "PASS"
    assert t.status_of("all-TCIN total") == "PASS"


@pytest.mark.bq
def test_one_broken_sku_fails_that_sku_and_the_totals(fixture_warehouse: Any) -> None:
    """Halving one SKU must go red in the SKU line AND in both totals it rolls into."""
    w = fixture_warehouse(
        sales_weekly=_sales_rows(halve_tcin=94928291),
        inventory_weekly=_inventory_rows(),
    )
    t = vk.run_checks(w)
    assert t.failed == 3, [line for line in t.lines if line[0] == "FAIL"]
    assert t.status_of("94928291 K-60WIP-DSN-COM-3PK") == "FAIL"
    assert t.status_of("all-TCIN total") == "FAIL"
    assert t.status_of("w/e 2026-06-06") == "FAIL"
    # Everything else is untouched — a broken SKU must not cascade.
    assert t.status_of("94928292 P-60WIP-DSN-CIT") == "PASS"
    assert t.status_of("w/e 2026-05-30") == "PASS"


@pytest.mark.bq
def test_known_report_discrepancy_is_reported_but_not_a_failure(
    fixture_warehouse: Any,
) -> None:
    """w/e 2026-04-04 at its pinned 23,994 units: KNOWN, printed, exit-clean."""
    w = fixture_warehouse(
        sales_weekly=_sales_rows(week_unit_overrides={"2026-04-04": 23994}),
        inventory_weekly=_inventory_rows(),
    )
    t = vk.run_checks(w)
    assert t.status_of("w/e 2026-04-04") == "KNOWN"
    assert (t.failed, t.known) == (0, 1)
    detail = next(d for s, lbl, d in t.lines if lbl.startswith("w/e 2026-04-04"))
    assert "23,994" in detail and "21,352" in detail
    assert "pinned" in detail


@pytest.mark.bq
def test_moving_off_the_pinned_value_is_a_real_failure(fixture_warehouse: Any) -> None:
    """The KNOWN allowance is not a blanket excuse for that week.

    If 2026-04-04 ever stops returning 23,994 the warehouse HAS moved, and the
    harness must say so — otherwise whitelisting the week would have created a
    permanent blind spot on the one week most likely to be re-cut upstream.
    """
    w = fixture_warehouse(
        sales_weekly=_sales_rows(week_unit_overrides={"2026-04-04": 30000}),
        inventory_weekly=_inventory_rows(),
    )
    t = vk.run_checks(w)
    assert t.status_of("w/e 2026-04-04") == "FAIL"
    assert (t.failed, t.known) == (1, 0)


@pytest.mark.bq
def test_missing_data_skips_rather_than_fails(fixture_warehouse: Any) -> None:
    """One correct historical week, no latest week, no channel, no inventory."""
    rows = [
        {
            "sales_date": "2026-05-30",
            "tcin": 88888888,
            "location_id": 1111,
            "sale_quantity": 34052.0,
            "sale_amount": 336867.0,
        }
    ]
    t = vk.run_checks(fixture_warehouse(sales_weekly=rows))
    assert t.failed == 0, [line for line in t.lines if line[0] == "FAIL"]
    assert t.status_of("w/e 2026-05-30") == "PASS"
    assert t.status_of("w/e 2026-06-06") == "SKIP"
    assert t.status_of("per-TCIN w/e 2026-06-06") == "SKIP"
    assert t.status_of("channel split") == "SKIP"
    assert t.status_of("inventory") == "SKIP"
    # 11 absent weeks + per-TCIN + channel + inventory
    assert t.skipped == 14


@pytest.mark.bq
def test_absent_sales_table_fails_loudly(fixture_warehouse: Any) -> None:
    """A registry without sales_weekly is a broken warehouse, not a clean run."""
    t = vk.run_checks(fixture_warehouse(inventory_weekly=_inventory_rows()))
    assert t.failed == 1
    assert t.status_of("sales_weekly") == "FAIL"
    assert t.passed == 0


# ---------------------------------------------------------------------------
# -m bq_live — the gate itself, against production
# ---------------------------------------------------------------------------


@pytest.mark.bq_live
@pytest.mark.parametrize(
    ("flags", "expected_exit"),
    [([], 0), (["--strict"], 1)],
    ids=["default", "strict"],
)
def test_gate_runs_against_production(flags: list[str], expected_exit: int) -> None:
    """End-to-end: real credentials, real registry, real numbers.

    Exit 0 by default because the only outstanding difference is the documented
    2026-04-04 report-side error; `--strict` refuses to tolerate even that. Both
    invocations run the same four queries (~95 MB on a cold cache).
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *flags],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    assert proc.returncode == expected_exit, proc.stdout + proc.stderr
    assert "KMG JunW1'26 tie-out" in proc.stdout
    assert "0 failed" in proc.stdout
    assert "[!] KNOWN w/e 2026-04-04" in proc.stdout
    # The tie-out must actually have run every check, not skipped its way to green.
    assert " 0 skipped" in proc.stdout
    assert "61 passed" in proc.stdout
