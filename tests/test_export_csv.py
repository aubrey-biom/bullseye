"""Tests for bpd_export_query_to_csv.

Two tiers (see conftest.py). Everything that writes a file executes real
BigQuery SQL over literal fixture CTEs (`@pytest.mark.bq`, 0 bytes billed).
The rejection paths — bad filename, non-SELECT SQL, a writable connection —
all return BEFORE the query is planned, so they run in the default tier with a
client that fails the test on any use.

What changed with the data layer: `Warehouse(path)` / `ReadOnlyView` are gone
(read-only is a property of the service-account credential now), `Settings` no
longer carries Kiteworks fields, and the export ceiling dropped from 1,000,000
to 200,000 rows because an unguarded export bills bytes rather than filling a
local disk.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from bpd_mcp.bq import BigQueryWarehouse
from bpd_mcp.config import Settings
from bpd_mcp.schemas import ExportQueryToCsvInput
from bpd_mcp.tools.query import export_query_to_csv

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class _NeverQueried:
    """Stands in for a `bigquery.Client` the code under test must not touch."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - only on failure
        raise AssertionError(f"BigQuery client was touched (.{name}) in an offline test")


def _offline_warehouse() -> BigQueryWarehouse:
    return BigQueryWarehouse(client=_NeverQueried(), registry={})


class _WritableWarehouse:
    """The one thing a real BigQueryWarehouse cannot be: `read_only` False."""

    read_only = False


def _settings(tmp_path: Path) -> Settings:
    """Settings pointed at a tmp data dir.

    Explicit kwargs outrank the environment in pydantic-settings, so this is
    isolated from BPD_DATA_DIR even though conftest sets one.
    """
    s = Settings(bpd_data_dir=str(tmp_path))
    s.ensure_dirs()
    return s


SALES_ROWS = [
    {"sales_date": "2026-05-02", "tcin": 100, "location_id": 2750,
     "sale_quantity": 50, "sale_amount": 150.0},
    {"sales_date": "2026-05-02", "tcin": 200, "location_id": 2750,
     "sale_quantity": 30, "sale_amount": 90.0},
    {"sales_date": "2026-05-09", "tcin": 300, "location_id": 2750,
     "sale_quantity": 10, "sale_amount": 40.0},
]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.bq
async def test_export_writes_the_query_result_verbatim(
    fixture_warehouse: Any, tmp_path: Path
) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_ROWS)
    s = _settings(tmp_path)
    resp = await export_query_to_csv(
        wh,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin, sale_quantity FROM sales_weekly ORDER BY tcin",
            filename="top_skus.csv",
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error

    p = Path(resp.data["path"])
    assert p.name == "top_skus.csv"
    assert p.parent == s.data_dir / "exports"
    assert resp.data["rows_written"] == 3
    assert resp.data["columns"] == ["tcin", "sale_quantity"]
    assert resp.data["bytes_written"] > 0
    # Fixture CTEs scan nothing, and the estimate is surfaced so cost shows up
    # in the transcript instead of on a bill.
    assert resp.data["estimated_bytes_scanned"] == 0

    with p.open() as f:
        rows = list(csv.reader(f))
    # sale_quantity is FLOAT64 in production, so it round-trips as "50.0".
    assert rows == [
        ["tcin", "sale_quantity"],
        ["100", "50.0"],
        ["200", "30.0"],
        ["300", "10.0"],
    ]


@pytest.mark.bq
async def test_export_can_omit_the_header_row(
    fixture_warehouse: Any, tmp_path: Path
) -> None:
    wh = fixture_warehouse(sales_weekly=SALES_ROWS)
    s = _settings(tmp_path)
    resp = await export_query_to_csv(
        wh,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin FROM sales_weekly ORDER BY tcin",
            filename="bare.csv",
            include_header=False,
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    with Path(resp.data["path"]).open() as f:
        assert list(csv.reader(f)) == [["100"], ["200"], ["300"]]


@pytest.mark.bq
async def test_export_max_rows_caps_the_result(
    fixture_warehouse: Any, tmp_path: Path
) -> None:
    """max_rows is enforced by wrapping the query, not by truncating the file —
    so the cap is applied by BigQuery and the reported count matches the file."""
    wh = fixture_warehouse(sales_weekly=SALES_ROWS)
    s = _settings(tmp_path)
    resp = await export_query_to_csv(
        wh,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin FROM sales_weekly ORDER BY tcin",
            filename="capped.csv",
            max_rows=2,
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["rows_written"] == 2
    with Path(resp.data["path"]).open() as f:
        assert list(csv.reader(f)) == [["tcin"], ["100"], ["200"]]


@pytest.mark.bq
async def test_export_file_mode_is_0644(
    fixture_warehouse: Any, tmp_path: Path
) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("POSIX-only")
    wh = fixture_warehouse(sales_weekly=SALES_ROWS)
    s = _settings(tmp_path)
    resp = await export_query_to_csv(
        wh,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin FROM sales_weekly",
            filename="perm.csv",
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert os.stat(Path(resp.data["path"])).st_mode & 0o777 == 0o644


@pytest.mark.bq
async def test_export_creates_the_exports_dir_when_missing(
    fixture_warehouse: Any, tmp_path: Path
) -> None:
    """No `ensure_dirs()` beforehand: the tool must create its own output dir."""
    wh = fixture_warehouse(sales_weekly=SALES_ROWS)
    s = Settings(bpd_data_dir=str(tmp_path / "fresh"))
    assert not s.exports_dir.exists()
    resp = await export_query_to_csv(
        wh,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin FROM sales_weekly",
            filename="made.csv",
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert Path(resp.data["path"]).parent == s.exports_dir
    assert s.exports_dir.is_dir()


# ---------------------------------------------------------------------------
# Rejection paths — all return before any query is planned
# ---------------------------------------------------------------------------


async def test_export_refuses_a_writable_connection(tmp_path: Path) -> None:
    resp = await export_query_to_csv(
        _WritableWarehouse(),  # type: ignore[arg-type]
        _settings(tmp_path),
        ExportQueryToCsvInput(
            sql="SELECT 1", filename="x.csv", response_format="json"
        ),
    )
    assert resp.ok is False
    assert resp.error.code == "SQL_BLOCKED"
    assert "read-only" in resp.error.message


@pytest.mark.parametrize(
    "blocked",
    [
        "DROP TABLE sales_weekly",
        "INSERT INTO sales_weekly VALUES (1)",
        "DELETE FROM sales_weekly",
        "CREATE TABLE evil AS SELECT 1",
        "UPDATE sales_weekly SET tcin = 1",
        "SELECT 1; DROP TABLE sales_weekly",
        # An export that writes somewhere else is exactly what this tool must
        # not become.
        "EXPORT DATA OPTIONS(uri='gs://leak/*.csv') AS SELECT 1",
    ],
)
async def test_export_rejects_write_sql(blocked: str, tmp_path: Path) -> None:
    resp = await export_query_to_csv(
        _offline_warehouse(),
        _settings(tmp_path),
        ExportQueryToCsvInput(
            sql=blocked, filename="x.csv", response_format="json"
        ),
    )
    assert resp.ok is False, f"failed to block: {blocked!r}"
    assert resp.error.code == "SQL_BLOCKED"
    assert not list((tmp_path / "exports").iterdir())


@pytest.mark.parametrize(
    "bad",
    ["../escape.csv", "subdir/file.csv", "/tmp/abs.csv", "back\\slash.csv"],
)
async def test_export_rejects_a_path_in_the_filename(bad: str, tmp_path: Path) -> None:
    s = _settings(tmp_path)
    resp = await export_query_to_csv(
        _offline_warehouse(),
        s,
        ExportQueryToCsvInput(
            sql="SELECT 1", filename=bad, response_format="json"
        ),
    )
    assert resp.ok is False, f"failed to reject: {bad!r}"
    assert resp.error.code == "INVALID_FILENAME"
    # Nothing escaped the exports dir, and nothing was written inside it.
    assert not list(s.exports_dir.iterdir())


@pytest.mark.parametrize("bad", ["results.txt", "data.json", "results", ".csv"])
async def test_export_rejects_a_non_csv_extension(bad: str, tmp_path: Path) -> None:
    resp = await export_query_to_csv(
        _offline_warehouse(),
        _settings(tmp_path),
        ExportQueryToCsvInput(
            sql="SELECT 1", filename=bad, response_format="json"
        ),
    )
    assert resp.ok is False, f"failed to reject: {bad!r}"
    assert resp.error.code == "INVALID_FILENAME"


async def test_export_checks_the_filename_before_the_sql(tmp_path: Path) -> None:
    """Ordering matters: a bad filename must not be masked by a SQL error, and
    a bad filename must never reach the (billable) dry run."""
    resp = await export_query_to_csv(
        _offline_warehouse(),
        _settings(tmp_path),
        ExportQueryToCsvInput(
            sql="DROP TABLE sales_weekly",
            filename="../evil.txt",
            response_format="json",
        ),
    )
    assert resp.ok is False
    assert resp.error.code == "INVALID_FILENAME"


@pytest.mark.bq
async def test_export_is_gated_by_the_byte_ceiling(
    bq_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost ceiling, against a REAL production table but still 0 bytes
    billed: the gate rejects on the dry-run estimate and nothing executes.
    A fixture CTE estimates 0 bytes and can never trip a positive limit, so
    this is the only honest way to exercise the guard."""
    real = BigQueryWarehouse(client=bq_client)
    s = _settings(tmp_path)
    tiny = Settings(bpd_bq_max_bytes_billed=1024, bpd_bq_warn_bytes=1024)
    monkeypatch.setattr("bpd_mcp.tools.query.get_settings", lambda: tiny)

    resp = await export_query_to_csv(
        real,
        s,
        ExportQueryToCsvInput(
            sql="SELECT tcin, sale_quantity FROM sales_daily",
            filename="expensive.csv",
            response_format="json",
        ),
    )
    assert resp.ok is False
    assert resp.error.code == "QUERY_TOO_EXPENSIVE"
    assert resp.error.details["estimated_bytes"] > 1024
    # And no partial file was left behind.
    assert not list(s.exports_dir.iterdir())
