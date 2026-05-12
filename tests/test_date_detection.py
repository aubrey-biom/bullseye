"""Date column detection tests (Patch #4, Issue 2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import SecretStr

from bpd_mcp.config import Settings
from bpd_mcp.schemas import CacheStatusInput
from bpd_mcp.tools.admin import cache_status
from bpd_mcp.warehouse import Warehouse


@pytest.mark.parametrize(
    ("column_name", "duckdb_type"),
    [
        # Suffix-style names Target uses heavily.
        ("fiscal_week_begin_d", "VARCHAR"),
        ("last_update_d", "DATE"),
        ("processed_ct_d", "VARCHAR"),
        ("snapshot_dt", "TIMESTAMP"),
        ("order_date", "DATE"),
        ("week_end_date", "DATE"),
        ("period_end_date", "DATE"),
        # Date-token names without _d suffix.
        ("report_date_dim", "DATE"),
        ("effective_date_dim", "DATE"),
        ("as_of_date", "DATE"),
    ],
)
def test_detect_date_column_picks_up_suffix_and_token_styles(
    tmp_path: Path, column_name: str, duckdb_type: str
) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.execute_sql(
            f"CREATE TABLE probe (tcin BIGINT, irrelevant_col TEXT, "
            f'"{column_name}" {duckdb_type})'
        )
        detected = wh.detect_date_column("probe")
        assert detected == column_name
    finally:
        wh.close()


def test_detect_date_column_prefers_typed_date_over_suffix(tmp_path: Path) -> None:
    """Tier 1 (DATE/TIMESTAMP type) beats tier 2 (name suffix) — even when the
    typed column doesn't match the name heuristic."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.execute_sql(
            "CREATE TABLE probe (created TIMESTAMP, week_end_d VARCHAR)"
        )
        # `created` is type TIMESTAMP — wins by tier 1.
        assert wh.detect_date_column("probe") == "created"
    finally:
        wh.close()


def test_detect_date_column_prefers_suffix_over_substring(tmp_path: Path) -> None:
    """Tier 2 (`_d`/`_dt`/`_date`) beats tier 3 (contains `week`/`period`/...)."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.execute_sql(
            "CREATE TABLE probe (week_label VARCHAR, fiscal_week_begin_d VARCHAR)"
        )
        # `_d` suffix wins over `week_label` even though both contain `week`.
        assert wh.detect_date_column("probe") == "fiscal_week_begin_d"
    finally:
        wh.close()


def test_detect_date_column_returns_none_for_truly_dateless_table(tmp_path: Path) -> None:
    wh = Warehouse(tmp_path / "bpd.duckdb")
    try:
        wh.execute_sql("CREATE TABLE probe (tcin BIGINT, name TEXT, price DOUBLE)")
        assert wh.detect_date_column("probe") is None
    finally:
        wh.close()


async def test_cache_status_handles_varchar_date_via_cast(tmp_path: Path) -> None:
    """A VARCHAR-stored ISO date still gets min/max'd correctly."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    wh.execute_sql(
        "CREATE TABLE forecast_weekly (tcin BIGINT, fiscal_week_begin_d VARCHAR, q BIGINT)"
    )
    wh.execute_sql(
        "INSERT INTO forecast_weekly VALUES "
        "(1, '2026-04-13', 10), (1, '2026-05-04', 20)"
    )
    s = Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        bpd_data_dir=str(tmp_path),
    )
    s.ensure_dirs()
    try:
        resp = await cache_status(wh, s, CacheStatusInput(response_format="json"))
    finally:
        wh.close()
    fw = next(r for r in resp.data["per_dataset"] if r["dataset"] == "forecast_weekly")
    assert fw["date_column"] == "fiscal_week_begin_d"
    assert fw["min_date"] == date(2026, 4, 13)
    assert fw["max_date"] == date(2026, 5, 4)


async def test_cache_status_transactional_vs_all_dimensional_split(tmp_path: Path) -> None:
    """The transactional-only date range must exclude location_attr's old dates."""
    wh = Warehouse(tmp_path / "bpd.duckdb")
    # Transactional table with recent dates.
    wh.execute_sql("CREATE TABLE sales_weekly (tcin BIGINT, sales_date DATE, q BIGINT)")
    wh.execute_sql(
        "INSERT INTO sales_weekly VALUES "
        "(1, DATE '2026-04-01', 10), (1, DATE '2026-05-01', 20)"
    )
    # Dimensional table with an ancient date (e.g. last_remodel_date back to 2000).
    wh.execute_sql(
        "CREATE TABLE location_attr (location_id BIGINT, last_remodel_date DATE)"
    )
    wh.execute_sql(
        "INSERT INTO location_attr VALUES (2750, DATE '2000-04-13')"
    )

    s = Settings(
        kiteworks_base_url="https://securesharek.target.com",
        kiteworks_username="u@example.com",
        kiteworks_password=SecretStr("pw"),
        bpd_data_dir=str(tmp_path),
    )
    s.ensure_dirs()
    try:
        resp = await cache_status(wh, s, CacheStatusInput(response_format="json"))
    finally:
        wh.close()

    data = resp.data
    # Transactional-only: sales_weekly only → April 2026 ↔ May 2026.
    assert data["earliest_data_date"] == date(2026, 4, 1)
    assert data["latest_data_date"] == date(2026, 5, 1)
    # Including dimensional: extends back to 2000 via location_attr.
    assert data["earliest_data_date_including_dimensional"] == date(2000, 4, 13)
    assert data["latest_data_date_including_dimensional"] == date(2026, 5, 1)
    # Per-dataset breakdown reports kind.
    by_ds = {r["dataset"]: r for r in data["per_dataset"]}
    assert by_ds["sales_weekly"]["kind"] == "transactional"
    assert by_ds["location_attr"]["kind"] == "dimensional"
