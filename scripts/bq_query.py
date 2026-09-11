"""Read-only BigQuery query runner for ad-hoc verification work.

    uv run python scripts/bq_query.py [--dry-run] [--limit N] [--json] FILE [FILE ...]

Runs each SQL file as ONE statement through the server's own data layer
(`BigQueryWarehouse`), so it inherits everything the MCP tools get:

  * the `sql_safety` validator (SELECT/WITH only, no DDL/DML tokens),
  * the pre-flight dry run and the hard `maximum_bytes_billed` cap,
  * CTE injection: a file may reference logical tables (`sales_daily`,
    `dtc_order_lines`, ...) by bare name exactly as `bpd_run_sql` allows.

Credentials are resolved IN MEMORY: `GCP_SA_KEY_B64` is decoded straight into
a `google.oauth2.service_account.Credentials` object and never written to disk.
(`resolve_credentials()` in bq.py materialises the key to ~/.config/gcloud for
the long-running server; a one-shot script has no reason to leave a key file
behind.) `GOOGLE_APPLICATION_CREDENTIALS`, when set, is used as-is.

Exit status: 0 on success, 1 on any failure. Output goes to stdout; this script
is never imported by the MCP server, so the stdio-transport stdout rule does
not apply here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bpd_mcp.bq import (  # noqa: E402
    BQ_LOCATION_DEFAULT,
    BQ_PROJECT_DEFAULT,
    BigQueryWarehouse,
    QueryTooExpensive,
)
from bpd_mcp.sql_safety import SqlBlocked, validate, wrap_with_limit  # noqa: E402


def _client(project: str, location: str) -> Any:
    from google.cloud import bigquery

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return bigquery.Client(project=project, location=location)
    b64 = os.environ.get("GCP_SA_KEY_B64")
    if not b64:
        sys.exit(
            "no BigQuery credential: set GCP_SA_KEY_B64 (base64 service-account JSON) "
            "or GOOGLE_APPLICATION_CREDENTIALS (path to the JSON)."
        )
    from google.oauth2 import service_account

    info = json.loads(base64.b64decode(b64, validate=True))
    creds = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(project=project, location=location, credentials=creds)


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _render_table(cols: list[str], rows: list[tuple[Any, ...]]) -> str:
    cells = [[str(c) for c in cols]] + [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cols))]

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) + " |"

    out = [fmt(cells[0]), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [fmt(r) for r in cells[1:]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("files", nargs="+", type=Path, help="SQL files, one statement each")
    ap.add_argument("--dry-run", action="store_true", help="validate and price only; bills 0 bytes")
    ap.add_argument("--limit", type=int, default=500, help="row cap wrapped around each query")
    ap.add_argument("--json", action="store_true", help="emit JSON lines instead of a table")
    ap.add_argument("--project", default=os.environ.get("BPD_BQ_PROJECT", BQ_PROJECT_DEFAULT))
    ap.add_argument("--location", default=os.environ.get("BPD_BQ_LOCATION", BQ_LOCATION_DEFAULT))
    ap.add_argument(
        "--max-bytes",
        type=int,
        default=int(os.environ.get("BPD_BQ_MAX_BYTES_BILLED", 2 * 1024**3)),
        help="hard maximum_bytes_billed per job (default 2 GiB; the server allows 20 GiB)",
    )
    args = ap.parse_args(argv)

    wh = BigQueryWarehouse(
        project=args.project,
        location=args.location,
        client=_client(args.project, args.location),
        maximum_bytes_billed=args.max_bytes,
    )

    failures = 0
    for path in args.files:
        sql = path.read_text().strip().rstrip(";")
        print(f"\n## {path}")
        try:
            validate(sql)
        except SqlBlocked as e:
            print(f"BLOCKED by sql_safety: {e}")
            failures += 1
            continue
        try:
            job = wh.dry_run(sql)
            print(f"dry run: {_human(job.total_bytes_processed)} would be scanned")
            if args.dry_run:
                continue
            cols, rows = wh.execute_sql(wrap_with_limit(sql, args.limit))
        except QueryTooExpensive as e:
            print(f"TOO EXPENSIVE: {e}")
            failures += 1
            continue
        except Exception as e:  # report, then keep going with the next file
            print(f"FAILED: {type(e).__name__}: {e}")
            failures += 1
            continue
        capped = f" (capped at --limit {args.limit})" if len(rows) >= args.limit else ""
        print(f"rows: {len(rows)}{capped}")
        if args.json:
            for r in rows:
                print(json.dumps(dict(zip(cols, r, strict=True)), default=str))
        else:
            print(_render_table(cols, rows))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
