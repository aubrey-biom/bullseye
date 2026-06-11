"""Tests for scripts/validate_kmg.py — the KMG POS-report tie-out harness.

The harness is the user's acceptance gate for the whole connector, so it gets
its own end-to-end test: seed a warehouse that matches the embedded KMG
expectations exactly, run the script as a subprocess, assert exit code 0.
Then break one number and assert exit code 1.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import duckdb

SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_kmg.py"


def _load_expectations():
    spec = importlib.util.spec_from_file_location("validate_kmg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_matching_warehouse(db: Path, *, break_one_tcin: bool = False) -> None:
    """Seed sales_weekly + inventory_weekly to match the KMG expectations."""
    mod = _load_expectations()
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, channel_originated VARCHAR, "
        "sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE inventory_weekly (tcin BIGINT, location_id BIGINT, "
        "report_date_dim VARCHAR, on_hand_units BIGINT)"
    )

    week = mod.WEEK
    store_frac = mod.EXPECTED_CHANNEL["store_originated_dollars"] / sum(
        mod.EXPECTED_CHANNEL.values()
    )

    sales_rows = []
    inv_rows = []
    for tcin, (_dpci, _model, units, dollars, oh) in mod.EXPECTED_TCIN.items():
        d = dollars if dollars is not None else 0.0
        u_store = round(units * store_frac)
        d_store = d * store_frac
        sales_rows.append((tcin, 1111, week, "STORE", u_store, d_store))
        sales_rows.append((tcin, 1111, week, "ONLINE", units - u_store, d - d_store))
        inv_rows.append((tcin, 1111, week, oh))
    # The report's TCIN=0 SKU (253-04-3809) — seed under a placeholder TCIN so
    # the weekly + all-TCIN totals line up.
    sales_rows.append(
        (
            77777777, 1111, week, "STORE",
            round(mod.UNMAPPED_DPCI_UNITS * store_frac),
            mod.UNMAPPED_DPCI_DOLLARS * store_frac,
        )
    )
    sales_rows.append(
        (
            77777777, 1111, week, "ONLINE",
            mod.UNMAPPED_DPCI_UNITS - round(mod.UNMAPPED_DPCI_UNITS * store_frac),
            mod.UNMAPPED_DPCI_DOLLARS * (1 - store_frac),
        )
    )
    # Other 11 weeks: aggregate dummy rows that hit the weekly totals exactly.
    for wk, units, dollars in mod.EXPECTED_WEEKS:
        if wk == week:
            continue
        sales_rows.append((88888888, 1111, wk, "STORE", units, dollars))

    if break_one_tcin:
        # Halve one SKU's units — should flip that check (and totals) to FAIL.
        sales_rows = [
            (t, loc, wk, ch, u // 2 if t == 94928291 else u, d)
            for (t, loc, wk, ch, u, d) in sales_rows
        ]

    conn.executemany("INSERT INTO sales_weekly VALUES (?,?,?,?,?,?)", sales_rows)
    conn.executemany("INSERT INTO inventory_weekly VALUES (?,?,?,?)", inv_rows)
    conn.close()


def _run(db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_harness_passes_on_matching_warehouse(tmp_path: Path) -> None:
    db = tmp_path / "bpd.duckdb"
    _seed_matching_warehouse(db)
    proc = _run(db)
    assert proc.returncode == 0, f"expected all-pass, got:\n{proc.stdout}\n{proc.stderr}"
    assert "0 failed" in proc.stdout


def test_harness_fails_on_broken_data(tmp_path: Path) -> None:
    db = tmp_path / "bpd.duckdb"
    _seed_matching_warehouse(db, break_one_tcin=True)
    proc = _run(db)
    assert proc.returncode == 1, f"expected failure exit, got:\n{proc.stdout}"
    assert "FAIL" in proc.stdout


def test_harness_skips_gracefully_on_missing_week(tmp_path: Path) -> None:
    """A warehouse with no 6/6 data must SKIP the per-TCIN checks, not crash."""
    db = tmp_path / "bpd.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE sales_weekly (tcin BIGINT, location_id BIGINT, "
        "sales_date DATE, sale_quantity BIGINT, sale_amount DOUBLE)"
    )
    # Only one (correct) historical week; no 6/6, no channel col, no inventory.
    conn.execute(
        "INSERT INTO sales_weekly VALUES (88888888, 1111, '2026-05-30', 34052, 336867.0)"
    )
    conn.close()
    proc = _run(db)
    assert proc.returncode == 0, f"missing data must skip, not fail:\n{proc.stdout}"
    assert "SKIP" in proc.stdout
