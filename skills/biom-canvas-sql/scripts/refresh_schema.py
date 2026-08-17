#!/usr/bin/env python3
"""Regenerate references/schema_map.md straight from INFORMATION_SCHEMA.

Replaces the CSV-dump-and-hand-back loop in refresh_schema.sh. That script
shells out to `bq`, which does not exist inside Claude Code, and it stopped at
dumping two CSVs for a human to paste back. This writes the finished file.

Runs anywhere the BigQuery Python client can authenticate:
  - Claude Code: reads GCP_SA_KEY_B64 from the environment (see setup_guide.md)
  - a laptop:    uses Application Default Credentials from `gcloud auth`

Usage:
    python scripts/refresh_schema.py                    # biom_canvas
    python scripts/refresh_schema.py --dataset biom_core
    python scripts/refresh_schema.py --stdout           # print, don't write

Read-only INFORMATION_SCHEMA scans; near-zero cost, nothing destructive.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

PROJECT = "biom-reporting-s26"
LOCATION = "us-central1"
REF = Path(__file__).resolve().parents[1] / "references"


def client():
    from google.cloud import bigquery

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        b64 = os.environ.get("GCP_SA_KEY_B64")
        if b64:
            dest = Path.home() / ".config" / "gcloud" / "biom-bq-sa.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(b64))
            dest.chmod(0o600)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(dest)
    # LOCATION matters: the default US multi-region returns ZERO rows from
    # INFORMATION_SCHEMA for these datasets, which reads as "empty warehouse"
    # rather than "wrong region".
    return bigquery.Client(project=PROJECT, location=LOCATION)


def q(c, sql: str) -> list:
    return list(c.query(sql).result())


def partition_and_cluster(ddl: str) -> str:
    """Pull PARTITION BY / CLUSTER BY back out of the DDL for the header line."""
    bits = []
    if m := re.search(r"PARTITION BY\s+(.+?)(?:\n|CLUSTER BY|OPTIONS)", ddl, re.S):
        bits.append(f"**Partitioned:** `{m.group(1).strip()}`")
    if m := re.search(r"CLUSTER BY\s+(.+?)(?:\n|OPTIONS)", ddl, re.S):
        bits.append(f"**Clustered:** `{m.group(1).strip()}`")
    return " · ".join(bits)


def view_body(ddl: str) -> str:
    """The SELECT out of a CREATE VIEW statement."""
    m = re.search(r"\bAS\b\s*(.+)$", ddl, re.S)
    return (m.group(1) if m else ddl).strip().rstrip(";")


def build(dataset: str) -> str:
    c = client()
    cols: dict[str, list] = {}
    for r in q(
        c,
        f"""SELECT table_name, ordinal_position, column_name, data_type, is_nullable
            FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
            ORDER BY table_name, ordinal_position""",
    ):
        cols.setdefault(r.table_name, []).append(r)

    meta = {
        r.table_name: r
        for r in q(
            c,
            f"""SELECT table_name, table_type, ddl
                FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES`""",
        )
    }

    tables = sorted(t for t, m in meta.items() if m.table_type != "VIEW")
    views = sorted(t for t, m in meta.items() if m.table_type == "VIEW")
    n_cols = sum(len(v) for v in cols.values())

    # An object is 🆕 — structure known, grain/quirks unverified — when the
    # judgment layer has never mentioned it. That flag is the whole point: it
    # stops anyone trusting a table this file merely lists.
    ref_path = REF / "database_reference.md"
    ref = ref_path.read_text() if ref_path.exists() else ""
    new = lambda t: "" if t in ref else " 🆕"  # noqa: E731

    out: list[str] = []
    add = out.append
    add("# Biom CANVAS — Structural Schema Map")
    add(
        f"**Project:** `{PROJECT}` · **Dataset:** `{dataset}` · "
        f"**Region:** `{LOCATION}`"
    )
    add(
        f"**Generated:** {datetime.now(UTC):%Y-%m-%d} from `INFORMATION_SCHEMA` · "
        f"**Objects:** {len(tables)} base tables, {len(views)} views · "
        f"**Columns:** {n_cols}"
    )
    add("")
    add(
        "> **This file is STRUCTURE, regenerable on demand — not hand-curated "
        "truth.** It goes stale as the warehouse changes. Regenerate with "
        "`python scripts/refresh_schema.py`. For grain, join keys, quirks, "
        "SUM-safe columns, and the correctness rules, see "
        "**`database_reference.md`** — that is the judgment layer. Objects "
        "flagged 🆕 appear in the live warehouse but are **not yet annotated** "
        "in `database_reference.md`: their structure is below but their "
        "grain/quirks are unverified — confirm before relying on them."
    )
    add("")
    add("## Contents")
    add("")
    add("**Base tables:** " + ", ".join(f"`{t}`{new(t)}" for t in tables))
    add("")
    add("**Views:** " + ", ".join(f"`{v}`{new(v)}" for v in views))
    add("")
    add("---")
    add("")
    add("## Base Tables")

    for t in tables:
        add("")
        add(f"### `{t}`{new(t)}")
        if pc := partition_and_cluster(meta[t].ddl or ""):
            add(pc)
        add("*Judgment/quirks: see `database_reference.md`.*")
        add("")
        add("| # | column | type | null |")
        add("|---|---|---|---|")
        for col in cols.get(t, []):
            null = "✓" if col.is_nullable == "YES" else ""
            add(
                f"| {col.ordinal_position} | `{col.column_name}` | "
                f"{col.data_type} | {null} |"
            )

    add("")
    add("---")
    add("")
    add("## Views (with logic)")
    add("")
    add(
        "View DDL is included because it encodes derivations and upstream "
        "lineage. Views already filter `is_current` internally — **do not "
        "re-add it** (Rule 1 exception in `database_reference.md`)."
    )

    for v in views:
        add("")
        add(f"### `{v}`{new(v)}")
        add("Columns: " + ", ".join(f"`{c_.column_name}`" for c_ in cols.get(v, [])))
        add("")
        add("```sql")
        add(view_body(meta[v].ddl or ""))
        add("```")

    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="biom_canvas")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    text = build(args.dataset)
    if args.stdout:
        print(text)
        return
    name = (
        "schema_map.md"
        if args.dataset == "biom_canvas"
        else f"schema_map_{args.dataset}.md"
    )
    dest = REF / name
    dest.write_text(text)
    print(f"wrote {dest} ({len(text.splitlines()):,} lines)")


if __name__ == "__main__":
    main()
