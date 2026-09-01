"""Tests for `bpd_health_check` after the move to read-only BigQuery.

The DuckDB-era suite drove each check by breaking local state — planting a `.ro`
snapshot, dropping a `_file_ledger` column, leaving an orphan zip. None of that
exists now. The checks that survived are the ones that were never really about
the file (`config_validity`, `mcp_self_check`, `roles_resolvable`,
`datasets_have_data`, `known_unpopulated_columns`, `tools_smoke_test`), plus
four born with the swap (`bq_credentials_present`, `location_configured`,
`bq_reachable_as`, `bq_datasets_reachable`, `registry_tables_resolve`,
`feed_freshness`).

Structure, and why:

  * Each check is a module-level coroutine, so each is driven DIRECTLY with a
    `FakeWarehouse` carrying exactly the broken state that check is for. That
    keeps every failure-path test free, offline, and specific — a fail-path test
    that needed a real broken BigQuery could not exist at all.
  * The runner is tested end to end twice: offline (`skip_network=True`, pure
    python) for name-set and aggregation behaviour, and once against live
    production data (`-m bq_live`) for the verdict that actually matters — "is
    the deployed server healthy right now?"

`FakeWarehouse` never executes SQL; see the note in test_tools_admin.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from test_tools_admin import FakeClient, FakeWarehouse

from bpd_mcp.bq import LOGICAL_TABLES, BigQueryWarehouse, CredentialsUnavailable, LogicalTable
from bpd_mcp.config import Settings
from bpd_mcp.schemas import HealthCheckInput
from bpd_mcp.tools import admin
from bpd_mcp.tools.admin import (
    EXPECTED_TOOL_COUNT,
    RETIRED_AFTER_DAYS,
    STALE_WARN_DAYS,
    health_check,
)


def _settings(tmp_path: Path, **over: Any) -> Settings:
    s = Settings(bpd_data_dir=str(tmp_path), **over)
    s.ensure_dirs()
    return s


def _logical(
    name: str,
    *,
    base: str = "biom-reporting-s26.bpd_raw.some_table",
    date_column: str = "business_d",
) -> LogicalTable:
    """A registry entry with a body nothing in these tests ever executes."""
    return LogicalTable(
        name=name,
        sql=f"SELECT 1 AS {date_column}",
        base_tables=(base,),
        date_column=date_column,
    )


@pytest.fixture()
def stub_credentials(monkeypatch: pytest.MonkeyPatch):
    """Pin `resolve_credentials` so the default tier needs no real credential.

    The runner calls it for `bq_credentials_present`; leaving it live would make
    these tests pass or fail on whether the machine happens to have a key.
    """
    monkeypatch.setattr(
        admin,
        "resolve_credentials",
        lambda: (Path("/tmp/sa.json"), "GCP_SA_KEY_B64 env var (materialised to /tmp/sa.json)"),
    )


def _by_name(resp) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in resp.data["rows"]}


# --------------------------------------------------------------------------------------
# The runner, offline
# --------------------------------------------------------------------------------------


async def test_skip_network_runs_only_the_local_checks(tmp_path: Path, stub_credentials) -> None:
    """`skip_network=True` must still produce a verdict, and say what it did not test.

    An operator reaches for this during an outage. A report that silently
    omitted the BigQuery checks would read as a clean bill of health.
    """
    wh = FakeWarehouse(registry=dict(LOGICAL_TABLES))
    resp = await health_check(
        warehouse=wh,
        settings=_settings(tmp_path),
        params=HealthCheckInput(skip_network=True, response_format="json"),
    )

    by = _by_name(resp)
    assert set(by) == {
        "bq_credentials_present",
        "location_configured",
        "config_validity",
        "mcp_self_check",
        "bigquery_checks_skipped",
    }
    assert by["bq_credentials_present"]["status"] == "pass"
    assert by["location_configured"]["status"] == "pass"
    assert by["config_validity"]["status"] == "pass"
    assert by["mcp_self_check"]["status"] == "pass"
    assert by["bigquery_checks_skipped"]["status"] == "warn"
    assert "Re-run without skip_network" in by["bigquery_checks_skipped"]["detail"]
    # One warn, no fail -> overall warn. A skipped audit is never a "pass".
    assert resp.data["overall_status"] == "warn"
    assert resp.data["summary"] == "overall=warn; pass=4 warn=1 fail=0"
    # Offline means offline: no metadata refresh, no query.
    assert wh.refreshed == 0
    assert wh.executed == []


async def test_health_check_records_duration_and_identity(tmp_path: Path, stub_credentials) -> None:
    wh = FakeWarehouse(registry=dict(LOGICAL_TABLES))
    resp = await health_check(
        warehouse=wh,
        settings=_settings(tmp_path),
        params=HealthCheckInput(skip_network=True, response_format="json"),
    )

    for row in resp.data["rows"]:
        assert isinstance(row["duration_ms"], int)
        assert row["duration_ms"] >= 0
    assert resp.data["warehouse"] == "bigquery://biom-reporting-s26/us-central1"
    assert resp.data["smoke_test_mode"] == "dry-run"
    assert isinstance(resp.data["timestamp"], datetime)


async def test_overall_status_is_fail_when_any_check_fails(tmp_path: Path, monkeypatch) -> None:
    """Aggregation: any fail dominates every warn."""
    monkeypatch.setattr(
        admin,
        "resolve_credentials",
        lambda: (_ for _ in ()).throw(CredentialsUnavailable("no credential")),
    )
    wh = FakeWarehouse(registry=dict(LOGICAL_TABLES))
    resp = await health_check(
        warehouse=wh,
        settings=_settings(tmp_path),
        params=HealthCheckInput(skip_network=True, response_format="json"),
    )

    by = _by_name(resp)
    assert by["bq_credentials_present"]["status"] == "fail"
    assert "GOOGLE_APPLICATION_CREDENTIALS" in by["bq_credentials_present"]["detail"]
    assert "GCP_SA_KEY_B64" in by["bq_credentials_present"]["detail"]
    assert resp.data["overall_status"] == "fail"


async def test_a_crashing_check_becomes_a_fail_not_a_traceback(tmp_path: Path, monkeypatch) -> None:
    """`_timed` must convert an exploding check into a reportable failure.

    A health check that raises takes down the one tool an operator runs when
    everything else is already broken.
    """
    def _boom() -> None:
        raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(admin, "resolve_credentials", _boom)
    wh = FakeWarehouse(registry=dict(LOGICAL_TABLES))
    resp = await health_check(
        warehouse=wh,
        settings=_settings(tmp_path),
        params=HealthCheckInput(skip_network=True, response_format="json"),
    )

    check = _by_name(resp)["bq_credentials_present"]
    assert check["status"] == "fail"
    assert "check raised: ZeroDivisionError" in check["detail"]
    assert resp.data["overall_status"] == "fail"


# --------------------------------------------------------------------------------------
# Local checks
# --------------------------------------------------------------------------------------


async def test_credentials_check_reports_source_but_never_key_material(stub_credentials) -> None:
    wh = FakeWarehouse()
    result = await admin._bq_credentials_present(warehouse=wh)

    assert result.status == "pass"
    assert "GCP_SA_KEY_B64 env var" in result.detail
    assert "/tmp/sa.json" in result.detail
    assert "BEGIN" not in result.detail  # a path, never the key


async def test_location_unset_is_a_failure_not_a_warning() -> None:
    """An unset location does not raise — it makes INFORMATION_SCHEMA return
    zero rows, which presents as "the table has no columns". Exactly the silent
    breakage a health check exists to make loud."""
    wh = FakeWarehouse(location="")
    result = await admin._location_configured(warehouse=wh, settings=Settings())

    assert result.status == "fail"
    assert "no BigQuery location" in result.detail


async def test_location_mismatch_with_settings_warns() -> None:
    wh = FakeWarehouse(location="us-east4")
    result = await admin._location_configured(warehouse=wh, settings=Settings())

    assert result.status == "warn"
    assert "us-east4" in result.detail
    assert "us-central1" in result.detail


async def test_config_validity_rejects_an_unreachable_warning_threshold(tmp_path: Path) -> None:
    """warn_bytes above max_bytes_billed is a config that cannot ever warn.

    The hard cap rejects the job first, so the operator gets a 500 with no
    preceding warning — the failure mode the warn threshold exists to prevent.
    """
    s = _settings(tmp_path, bpd_bq_warn_bytes=40 * 1024**3, bpd_bq_max_bytes_billed=20 * 1024**3)
    result = await admin._config_validity(warehouse=FakeWarehouse(), settings=s)

    assert result.status == "fail"
    assert "BPD_BQ_WARN_BYTES is above BPD_BQ_MAX_BYTES_BILLED" in result.detail


async def test_config_validity_reports_every_problem_at_once(tmp_path: Path) -> None:
    s = _settings(tmp_path, bpd_vendor_id="", bpd_bq_max_bytes_billed=0)
    result = await admin._config_validity(warehouse=FakeWarehouse(), settings=s)

    assert result.status == "fail"
    assert "BPD_VENDOR_ID is not set" in result.detail
    assert "BPD_BQ_MAX_BYTES_BILLED must be positive" in result.detail


async def test_config_validity_passes_and_states_the_cost_ceiling(tmp_path: Path) -> None:
    result = await admin._config_validity(warehouse=FakeWarehouse(), settings=_settings(tmp_path))

    assert result.status == "pass"
    assert "max_bytes_billed=20 GiB" in result.detail
    assert "warn at 1 GiB" in result.detail
    assert "vendor=139440/BV" in result.detail


async def test_mcp_self_check_counts_the_registered_tools() -> None:
    result = await admin._mcp_self_check()

    assert result.status == "pass"
    assert str(EXPECTED_TOOL_COUNT) in result.detail


async def test_mcp_self_check_fails_when_tools_are_missing(monkeypatch) -> None:
    """The failure mode: a registration raised at import and the server came up
    with fewer tools than it advertises."""
    monkeypatch.setattr(admin, "EXPECTED_TOOL_COUNT", 99)
    result = await admin._mcp_self_check()

    assert result.status == "fail"
    assert "/99 tools registered" in result.detail


async def test_mcp_self_check_warns_when_a_tool_was_added_without_bumping_the_count(
    monkeypatch,
) -> None:
    monkeypatch.setattr(admin, "EXPECTED_TOOL_COUNT", 2)
    result = await admin._mcp_self_check()

    assert result.status == "warn"
    assert "without bumping the expected count" in result.detail


# --------------------------------------------------------------------------------------
# registry_tables_resolve — the new central failure surface
# --------------------------------------------------------------------------------------


def _registry_warehouse(**over: Any) -> FakeWarehouse:
    registry = {
        "sales_daily": _logical(
            "sales_daily",
            base="biom-reporting-s26.biom_canvas.fct_target_sales",
            date_column="sales_date",
        ),
        "item_attr": _logical(
            "item_attr",
            base="biom-reporting-s26.bpd_raw.weekly_item_mta",
            date_column="processed_ct_date",
        ),
    }
    kwargs: dict[str, Any] = {
        "registry": registry,
        "row_counts": {
            "biom-reporting-s26.biom_canvas.fct_target_sales": 489_420,
            "biom-reporting-s26.bpd_raw.weekly_item_mta": 2_220,
        },
        "schemas": {
            "sales_daily": [("sales_date", "DATE"), ("tcin", "INT64"), ("sale_quantity", "FLOAT64")],
            "item_attr": [("processed_ct_date", "DATE"), ("tcin", "INT64")],
        },
        "sql_results": {
            "data_grain": (
                ["src", "data_grain"],
                [
                    ("biom-reporting-s26.biom_canvas.fct_target_sales", "daily"),
                    ("biom-reporting-s26.biom_canvas.fct_target_sales", "weekly"),
                    ("biom-reporting-s26.biom_canvas.fct_target_sales", "history_weekly"),
                ],
            )
        },
    }
    kwargs.update(over)
    return FakeWarehouse(**kwargs)


async def test_registry_tables_resolve_passes_and_reports_zero_cost() -> None:
    result = await admin._registry_tables_resolve(warehouse=_registry_warehouse())

    assert result.status == "pass"
    assert "all 2 logical table(s) dry-run clean" in result.detail
    assert "0 bytes billed" in result.detail
    assert "data_grain values as expected" in result.detail


async def test_registry_tables_resolve_fails_when_a_base_table_vanishes() -> None:
    """A source table renamed upstream shows up HERE, once, instead of in every
    analytics tool separately."""
    wh = _registry_warehouse(
        row_counts={"biom-reporting-s26.bpd_raw.weekly_item_mta": 2_220}  # canvas fact gone
    )
    result = await admin._registry_tables_resolve(warehouse=wh)

    assert result.status == "fail"
    assert "base table missing: sales_daily -> biom-reporting-s26.biom_canvas.fct_target_sales" in result.detail


async def test_registry_tables_resolve_fails_when_a_body_stops_compiling() -> None:
    class BrokenSchema(FakeWarehouse):
        def logical_schema(self, table: str) -> list[tuple[str, str]]:
            if table == "sales_daily":
                raise RuntimeError("400 Unrecognized name: sale_quantity")
            return super().logical_schema(table)

    wh = _registry_warehouse()
    broken = BrokenSchema(
        registry=wh.registry,
        row_counts=wh.base_row_counts(),
        schemas={"item_attr": [("processed_ct_date", "DATE")]},
    )
    result = await admin._registry_tables_resolve(warehouse=broken)

    assert result.status == "fail"
    assert "sales_daily: RuntimeError" in result.detail
    assert "Unrecognized name" in result.detail


async def test_registry_tables_resolve_fails_when_a_body_projects_nothing() -> None:
    wh = _registry_warehouse(schemas={"sales_daily": [], "item_attr": [("tcin", "INT64")]})
    result = await admin._registry_tables_resolve(warehouse=wh)

    assert result.status == "fail"
    assert "sales_daily: body compiled but projects no columns" in result.detail


async def test_registry_tables_resolve_warns_on_an_unexpected_data_grain() -> None:
    """`data_grain` is an unconstrained STRING upstream, and sales_weekly filters
    on an explicit value list — a fourth value would be silently dropped rather
    than reported, which is a wrong number, not an error."""
    wh = _registry_warehouse(
        sql_results={
            "data_grain": (
                ["src", "data_grain"],
                [
                    ("biom-reporting-s26.biom_canvas.fct_target_sales", "daily"),
                    ("biom-reporting-s26.biom_canvas.fct_target_sales", "monthly"),
                ],
            )
        }
    )
    result = await admin._registry_tables_resolve(warehouse=wh)

    assert result.status == "warn"
    assert "data_grain='monthly'" in result.detail
    assert "silently ignore" in result.detail


# --------------------------------------------------------------------------------------
# roles_resolvable
# --------------------------------------------------------------------------------------


async def test_roles_resolvable_fails_when_a_required_role_has_no_column() -> None:
    """The P0-1 regression, restated for BigQuery: a POPULATED table whose real
    columns match no candidate for a required role. Four analytics tools would
    hard-fail; health must say so before the user finds out."""
    wh = FakeWarehouse(
        registry={"inventory_daily": _logical(
            "inventory_daily", base="biom-reporting-s26.biom_canvas.fct_target_inventory"
        )},
        row_counts={"biom-reporting-s26.biom_canvas.fct_target_inventory": 1_000},
        schemas={
            "inventory_daily": [
                ("business_d", "DATE"),
                ("tcin", "INT64"),
                ("location_id", "INT64"),
                ("weird_stock_metric", "INT64"),
            ]
        },
    )
    result = await admin._roles_resolvable(warehouse=wh)

    assert result.status == "fail"
    assert "inventory_daily.on_hand" in result.detail
    # The diagnostic must carry both halves of the fix.
    assert "ending_on_hand_q" in result.detail  # candidates tried
    assert "weird_stock_metric" in result.detail  # what the table actually has
    assert "column_roles.COLUMN_ROLES" in result.detail
    assert "bq.LOGICAL_TABLES" in result.detail


async def test_roles_resolvable_warns_not_fails_for_listing_only_roles() -> None:
    """An orders_daily generation without the ETA columns breaks the dataset
    listing's content range and nothing else — a warn with accurate wording, not
    a false 'analytics tools WILL fail'."""
    wh = FakeWarehouse(
        registry={"orders_daily": _logical(
            "orders_daily",
            base="biom-reporting-s26.bpd_raw.daily_order_tcin_loc",
            date_column="snapshot_d",
        )},
        row_counts={"biom-reporting-s26.bpd_raw.daily_order_tcin_loc": 147_166},
        schemas={
            "orders_daily": [
                ("snapshot_d", "DATE"),
                ("purchase_order_id", "INT64"),
                ("purchase_order_create_d", "DATE"),
                ("tcin", "INT64"),
                ("receiving_location_id", "INT64"),
                ("revised_order_q", "INT64"),
                ("item_received_q", "FLOAT64"),
                ("cancel_remaining_order_q", "INT64"),
            ]
        },
    )
    result = await admin._roles_resolvable(warehouse=wh)

    assert result.status == "warn"
    assert "listing-only" in result.detail
    assert "orders_daily.eta" in result.detail
    assert "no analytics tool fails" in result.detail


async def test_roles_resolvable_passes_on_real_column_names() -> None:
    wh = FakeWarehouse(
        registry={"orders_daily": _logical(
            "orders_daily",
            base="biom-reporting-s26.bpd_raw.daily_order_tcin_loc",
            date_column="snapshot_d",
        )},
        row_counts={"biom-reporting-s26.bpd_raw.daily_order_tcin_loc": 147_166},
        schemas={
            "orders_daily": [
                ("snapshot_d", "DATE"),
                ("purchase_order_id", "INT64"),
                ("purchase_order_create_d", "DATE"),
                ("tcin", "INT64"),
                ("receiving_location_id", "INT64"),
                ("revised_order_q", "INT64"),
                ("item_received_q", "FLOAT64"),
                ("cancel_remaining_order_q", "INT64"),
                ("revised_estimated_arrival_d", "DATE"),
            ]
        },
    )
    result = await admin._roles_resolvable(warehouse=wh)

    assert result.status == "pass", result.detail


async def test_roles_resolvable_skips_a_table_whose_base_is_empty() -> None:
    """An empty base table cannot prove a role is missing — it proves nothing.

    (And the emptiness probe reads `__TABLES__`, never `COUNT(*)` through the
    CTE, which across the roster would cost ~333 MB per health check.)
    """
    wh = FakeWarehouse(
        registry={"inventory_daily": _logical(
            "inventory_daily", base="biom-reporting-s26.biom_canvas.fct_target_inventory"
        )},
        row_counts={"biom-reporting-s26.biom_canvas.fct_target_inventory": 0},
        schemas={"inventory_daily": [("nothing_useful", "STRING")]},
    )
    result = await admin._roles_resolvable(warehouse=wh)

    assert result.status == "pass"
    assert wh.executed == []  # no COUNT(*) anywhere


# --------------------------------------------------------------------------------------
# datasets_have_data
# --------------------------------------------------------------------------------------


def _dataset_status_rows(**statuses: str) -> list[dict[str, Any]]:
    return [{"dataset": name, "status": status} for name, status in statuses.items()]


async def test_datasets_have_data_reports_retired_empties_as_informational() -> None:
    """An empty RETIRED dataset (feed sunset by Target) must not drive the warn
    wording — there is no data to expect and nothing to fix."""
    wh = FakeWarehouse(
        registry={
            "sales_weekly": _logical("sales_weekly", base="biom-reporting-s26.biom_canvas.fct_target_sales"),
            "sales_weekly_item": _logical(
                "sales_weekly_item", base="biom-reporting-s26.bpd_raw.weekly_sales_tcin"
            ),
        },
        row_counts={
            "biom-reporting-s26.biom_canvas.fct_target_sales": 489_420,
            "biom-reporting-s26.bpd_raw.weekly_sales_tcin": 0,
        },
        datasets=_dataset_status_rows(sales_weekly="active", sales_weekly_item="retired"),
    )
    result = await admin._datasets_have_data(warehouse=wh)

    assert result.status == "pass"
    assert "1/2 dataset(s) populated" in result.detail
    assert "empty-but-retired" in result.detail
    assert "sales_weekly_item" in result.detail


async def test_datasets_have_data_warns_on_an_empty_active_dataset() -> None:
    wh = FakeWarehouse(
        registry={
            "sales_daily": _logical("sales_daily", base="biom-reporting-s26.biom_canvas.fct_target_sales"),
        },
        row_counts={"biom-reporting-s26.biom_canvas.fct_target_sales": 0},
        datasets=_dataset_status_rows(sales_daily="active"),
    )
    result = await admin._datasets_have_data(warehouse=wh)

    assert result.status == "warn"
    assert "empty active dataset(s): ['sales_daily']" in result.detail
    # Points at the upstream pipeline, which is where the fix lives.
    assert "bpd_meta.ingestion_state" in result.detail
    assert "bpd_data_freshness" in result.detail


# --------------------------------------------------------------------------------------
# feed_freshness
# --------------------------------------------------------------------------------------


def _pattern(name: str, lag: int, *, files: int = 10) -> dict[str, Any]:
    return {
        "pattern": name,
        "files": files,
        "max_file_date": date(2026, 9, 1),
        "max_downloaded_at": datetime(2026, 9, 1, 6, 47, tzinfo=UTC),
        "total_bytes": 100,
        "lag_days": lag,
        "logical_tables": [],
    }


def _freshness(per_pattern: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "per_pattern": per_pattern,
        "last_ingest_at": datetime(2026, 9, 1, 6, 47, tzinfo=UTC),
        "total_files": sum(p["files"] for p in per_pattern),
        "patterns_seen": len(per_pattern),
    }


async def test_feed_freshness_passes_when_every_active_pattern_is_current() -> None:
    wh = FakeWarehouse(freshness=_freshness([_pattern("DAILY_SALES_TCIN_LOC", 1)]))
    result = await admin._feed_freshness(warehouse=wh)

    assert result.status == "pass"
    assert f"within {STALE_WARN_DAYS}d" in result.detail


async def test_feed_freshness_warns_on_a_stale_pattern_and_blames_the_pipeline() -> None:
    wh = FakeWarehouse(
        freshness=_freshness(
            [_pattern("DAILY_SALES_TCIN_LOC", 1), _pattern("WEEKLY_ITEM_MTA", STALE_WARN_DAYS + 21)]
        )
    )
    result = await admin._feed_freshness(warehouse=wh)

    assert result.status == "warn"
    assert "WEEKLY_ITEM_MTA" in result.detail
    assert "upstream pipeline condition, not an MCP fault" in result.detail


async def test_feed_freshness_treats_a_long_dead_pattern_as_retired_not_stale() -> None:
    """Target has genuinely sunset several item-grain rollups. Reporting them as
    broken forever trains the operator to ignore the check."""
    wh = FakeWarehouse(
        freshness=_freshness(
            [_pattern("DAILY_SALES_TCIN_LOC", 1), _pattern("WEEKLY_SALES_TCIN", RETIRED_AFTER_DAYS + 18)]
        )
    )
    result = await admin._feed_freshness(warehouse=wh)

    assert result.status == "pass"
    assert f"retired (>{RETIRED_AFTER_DAYS}d): ['WEEKLY_SALES_TCIN" in result.detail


async def test_feed_freshness_fails_when_the_ledger_is_empty() -> None:
    wh = FakeWarehouse(freshness=_freshness([]))
    result = await admin._feed_freshness(warehouse=wh)

    assert result.status == "fail"
    assert "bpd_meta.ingestion_state is empty" in result.detail


# --------------------------------------------------------------------------------------
# known_unpopulated_columns
# --------------------------------------------------------------------------------------


def _unpopulated_warehouse(total: int, placeholder: int) -> FakeWarehouse:
    return FakeWarehouse(
        registry={"orders_daily": _logical(
            "orders_daily",
            base="biom-reporting-s26.bpd_raw.daily_order_tcin_loc",
            date_column="snapshot_d",
        )},
        schemas={
            "orders_daily": [("snapshot_d", "DATE"), ("purchase_order_active_f", "STRING")]
        },
        sql_results={"COUNTIF": (["total", "placeholder"], [(total, placeholder)])},
    )


async def test_known_unpopulated_columns_passes_while_mostly_placeholder() -> None:
    """`purchase_order_active_f` holds three values today ('""' x144,332,
    'true' x1,861, '' x973). The test is "at least MOSTLY placeholder", not
    "exclusively" — an exclusivity test would warn forever."""
    wh = _unpopulated_warehouse(total=147_166, placeholder=145_305)
    result = await admin._known_unpopulated_columns(warehouse=wh)

    assert result.status == "pass"
    assert "1 known-unpopulated source column(s)" in result.detail
    assert "95%" in result.detail


async def test_known_unpopulated_columns_warns_when_target_starts_populating() -> None:
    wh = _unpopulated_warehouse(total=100, placeholder=50)
    result = await admin._known_unpopulated_columns(warehouse=wh)

    assert result.status == "warn"
    assert "orders_daily.purchase_order_active_f" in result.detail
    assert "50.0% real values of 100 rows" in result.detail
    # And the advice must NOT be "start filtering on it": at 98% placeholder a
    # filter drops nearly the entire order book.
    assert "do NOT start" in result.detail


async def test_known_unpopulated_columns_ignores_a_column_the_body_stopped_projecting() -> None:
    wh = FakeWarehouse(
        registry={"orders_daily": _logical(
            "orders_daily",
            base="biom-reporting-s26.bpd_raw.daily_order_tcin_loc",
            date_column="snapshot_d",
        )},
        schemas={"orders_daily": [("snapshot_d", "DATE")]},
    )
    result = await admin._known_unpopulated_columns(warehouse=wh)

    assert result.status == "pass"
    assert wh.executed == []  # nothing to count, nothing queried


# --------------------------------------------------------------------------------------
# Live: the verdict on the deployed server
# --------------------------------------------------------------------------------------


@pytest.mark.bq_live
async def test_full_health_check_against_production(tmp_path: Path) -> None:
    """The whole runner over real production data — the only test that answers
    "is the deployed server healthy right now?".

    BILLS BYTES: the date-range sweep (~527 MB, shared with bpd_list_datasets)
    plus one COUNTIF pass for the known-unpopulated guard. The tool smoke test
    stays in dry-run mode (0 bytes), which is what it is for.
    """
    wh = BigQueryWarehouse()
    try:
        resp = await health_check(
            warehouse=wh,
            settings=_settings(tmp_path),
            params=HealthCheckInput(skip_network=False, response_format="json"),
        )
    finally:
        wh.close()

    by = _by_name(resp)
    assert set(by) == {
        "bq_credentials_present",
        "location_configured",
        "config_validity",
        "mcp_self_check",
        "bq_reachable_as",
        "bq_datasets_reachable",
        "registry_tables_resolve",
        "roles_resolvable",
        "datasets_have_data",
        "feed_freshness",
        "known_unpopulated_columns",
        "tools_smoke_test",
    }
    failed = {n: r["detail"] for n, r in by.items() if r["status"] == "fail"}
    assert failed == {}, f"health check failing against production: {failed}"

    # The checks that must be actively PASSING, not merely not-failing.
    assert by["bq_reachable_as"]["status"] == "pass"
    assert by["bq_datasets_reachable"]["status"] == "pass"
    assert by["registry_tables_resolve"]["status"] == "pass"
    assert by["roles_resolvable"]["status"] == "pass"
    assert by["tools_smoke_test"]["status"] == "pass"
    # Every tool in the roster compiled as real BigQuery, at zero cost.
    detail = by["tools_smoke_test"]["detail"]
    assert "[dry-run]" in detail
    assert "bpd_get_forecast_vs_actual" in detail
    assert "0 bytes billed" in detail
    assert f"all {len(LOGICAL_TABLES)} logical table(s)" in by["registry_tables_resolve"]["detail"]


@pytest.mark.bq_live
async def test_registry_and_roles_resolve_against_live_schemas() -> None:
    """The two 0-byte checks that catch an upstream column rename, run for real.

    Cheap enough to keep separate from the full run above: `__TABLES__` counts
    and cached dry-run schemas only. (Marked bq_live rather than bq because the
    registry it validates is the PRODUCTION one — a fixture registry would be
    validating the fixtures.)
    """
    wh = BigQueryWarehouse()
    try:
        registry_result = await admin._registry_tables_resolve(warehouse=wh)
        roles_result = await admin._roles_resolvable(warehouse=wh)
    finally:
        wh.close()

    assert registry_result.status == "pass", registry_result.detail
    assert "0 bytes billed" in registry_result.detail
    assert roles_result.status == "pass", roles_result.detail


@pytest.mark.bq
async def test_datasets_reachable_names_every_dataset_the_registry_reads(bq_client) -> None:
    """0 bytes: a dataset listing, no query. Guards the three-dataset topology —
    a lost dataViewer grant on bpd_meta shows up here, not as a confusing empty
    freshness report."""
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    result = await admin._bq_datasets_reachable(warehouse=wh)

    assert result.status == "pass", result.detail
    for dataset in ("biom_canvas", "bpd_raw", "bpd_meta"):
        assert dataset in result.detail


async def test_datasets_reachable_fails_when_a_dataset_is_missing() -> None:
    """The offline half of the check above: bpd_meta unreachable must FAIL."""
    wh = FakeWarehouse(
        registry=dict(LOGICAL_TABLES),
        client=FakeClient(("biom_canvas", "bpd_raw")),  # no bpd_meta
    )
    result = await admin._bq_datasets_reachable(warehouse=wh)

    assert result.status == "fail"
    assert "bpd_meta" in result.detail
    assert "dataViewer" in result.detail


async def test_bq_reachable_as_warns_on_an_unexpected_identity() -> None:
    """Querying as a personal login instead of the read-only service account is
    not an error, but it means the cost and the permissions are someone else's."""
    wh = FakeWarehouse(
        sql_results={"SESSION_USER()": (["session_user"], [("aubrey@getbiom.co",)])}
    )
    result = await admin._bq_reachable_as(warehouse=wh)

    assert result.status == "warn"
    assert "aubrey@getbiom.co" in result.detail
    assert "GOOGLE_APPLICATION_CREDENTIALS" in result.detail


async def test_bq_reachable_as_fails_when_the_query_does_not_round_trip() -> None:
    wh = FakeWarehouse(
        sql_results={"SESSION_USER()": ConnectionError("503 Service Unavailable")}
    )
    result = await admin._bq_reachable_as(warehouse=wh)

    assert result.status == "fail"
    assert "cannot query BigQuery" in result.detail
    assert "503" in result.detail
