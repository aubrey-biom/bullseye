"""`bpd-sync` — headless daily sync for cron/launchd (Patch #15).

Runs the exact same pipeline as the `bpd_sync_new_files` MCP tool, without an
LLM in the loop: discover → download → parse → load → (optional) health check,
then print one summary line and exit with a meaningful status code.

Why this exists: the warehouse on this machine is the system of record and the
ONLY archive of history older than Kiteworks' ~2-week retention. Keeping it
fresh must not depend on someone remembering to open Claude Desktop.

DuckDB is single-writer, so if the MCP server is running (Claude Desktop open)
it already holds the write lock. That is expected, not an error: the run
retries a few times, then exits 75 (EX_TEMPFAIL) and leaves the work for the
next scheduled run — syncs are idempotent, so nothing is lost by skipping one.

Exit codes:
    0   success (all discovered files loaded or already current)
    1   sync completed but one or more files FAILED to load
    2   sync succeeded; the post-sync health check reported overall=fail
    3   fatal error (auth, config, network, unexpected exception)
    75  warehouse locked by another process — skipped, try again later
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

EXIT_OK = 0
EXIT_FILES_FAILED = 1
EXIT_HEALTH_FAIL = 2
EXIT_FATAL = 3
EXIT_LOCKED = 75


def _is_lock_error(exc: BaseException) -> bool:
    """Is this DuckDB refusing a second writer (i.e. the MCP server is up)?"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "conflicting lock",
            "could not set lock",
            "being used by another process",
            "file is already open",
        )
    )


async def _run_once(args: argparse.Namespace) -> int:
    # Imported lazily so `--help` stays fast and import errors surface cleanly.
    import duckdb

    from .auth import AuthManager
    from .client import KiteworksClient, make_http_client
    from .config import get_settings
    from .logging_setup import configure_logging, get_logger
    from .schemas import HealthCheckInput
    from .sync import sync_new_files
    from .tools.admin import health_check
    from .warehouse import Warehouse

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.bpd_log_level, settings.log_dir)
    logger = get_logger("bpd_mcp.cli_sync")

    datasets = set(args.datasets) if args.datasets else None
    started = datetime.now(UTC)

    http = make_http_client(settings)
    warehouse: Warehouse | None = None
    try:
        try:
            warehouse = Warehouse(settings.db_path, read_only=False)
        except duckdb.Error as e:
            if _is_lock_error(e):
                logger.warning("warehouse_locked", error=str(e))
                print(
                    "bpd-sync: warehouse is locked by another process "
                    "(the MCP server is probably running) — skipping this run.",
                    file=sys.stderr,
                )
                return EXIT_LOCKED
            raise

        auth = AuthManager.load_from_disk(settings, http)
        client = KiteworksClient(settings, auth, http)

        result = await sync_new_files(
            client,
            warehouse,
            settings,
            datasets=datasets,
            triggered_by=args.triggered_by,
            dry_run=args.dry_run,
        )

        overall = "skipped"
        if not args.dry_run and not args.skip_health:
            health = await health_check(
                auth=auth,
                client=client,
                warehouse=warehouse,
                settings=settings,
                params=HealthCheckInput(skip_network=True, response_format="json"),
            )
            overall = str((health.data or {}).get("overall_status", "unknown"))

        summary: dict[str, Any] = {
            "started_at": started.isoformat(),
            "duration_s": round((datetime.now(UTC) - started).total_seconds(), 2),
            "found": result.files_found,
            "loaded": result.files_loaded,
            "failed": result.files_failed,
            "skipped": result.files_skipped,
            "unknown": result.files_unknown,
            "health": overall,
            "dry_run": args.dry_run,
        }
        logger.info("cli_sync_complete", **summary)
        print(
            "bpd-sync {started_at} in {duration_s}s: found={found} loaded={loaded} "
            "failed={failed} skipped={skipped} unknown={unknown} health={health}"
            "{dry}".format(**summary, dry=" (dry-run)" if args.dry_run else "")
        )
        for outcome in result.outcomes:
            if outcome.status == "failed":
                print(
                    f"  FAILED {outcome.file_name}: {outcome.error}", file=sys.stderr
                )

        if result.files_failed:
            return EXIT_FILES_FAILED
        if overall == "fail":
            return EXIT_HEALTH_FAIL
        return EXIT_OK
    finally:
        if warehouse is not None:
            warehouse.close()
        await http.aclose()


async def _run(args: argparse.Namespace) -> int:
    """Run, retrying only the transient locked-warehouse case."""
    for attempt in range(1, max(1, args.lock_retries) + 1):
        code = await _run_once(args)
        if code != EXIT_LOCKED or attempt >= max(1, args.lock_retries):
            return code
        await asyncio.sleep(args.lock_retry_delay)
    return EXIT_LOCKED


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bpd-sync",
        description=(
            "Headless BPD sync for cron/launchd. Downloads and loads any new or "
            "restated Kiteworks files into the local DuckDB warehouse."
        ),
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        metavar="DATASET",
        help="Limit the sync to these datasets (default: all active feeds).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be processed without downloading or loading.",
    )
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="Skip the post-sync health check (faster; no fail signal).",
    )
    parser.add_argument(
        "--triggered-by",
        default="bpd-sync-cli",
        help="Value recorded in the _sync_log audit trail (default: %(default)s).",
    )
    parser.add_argument(
        "--lock-retries",
        type=int,
        default=3,
        help=(
            "How many times to retry when the warehouse is locked by the "
            "running MCP server (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--lock-retry-delay",
        type=float,
        default=60.0,
        help="Seconds between locked-warehouse retries (default: %(default)s).",
    )
    args = parser.parse_args()

    try:
        raise SystemExit(asyncio.run(_run(args)))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(EXIT_FATAL) from None
    except Exception as e:
        print(f"bpd-sync: fatal: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(EXIT_FATAL) from e


if __name__ == "__main__":
    main()
