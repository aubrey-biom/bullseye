"""Admin-tool tests: bpd_list_datasets, bpd_bigquery_status, bpd_data_freshness,
bpd_describe_schema.

Ported from the DuckDB era, where these tests seeded a local file with
`CREATE TABLE` and read disk statistics back. Neither half survives: there is no
local store to seed, and `bpd_clear_cache` — which had its own tests — is gone
rather than stubbed.

What these tools actually contain, once the data layer is a network service, is
PYTHON logic over metadata: the transactional/dimensional split, the snapshot vs
content horizon, degradation when one BigQuery probe fails but the others
succeed, and the rendering of a row count whose basis is not what a reader
assumes. That logic is what is tested here, against `FakeWarehouse` — a
metadata-only stand-in that executes no SQL, so the default tier stays free.

`FakeWarehouse` is deliberately NOT a query engine (see conftest's note on why a
DuckDB double was rejected). It never parses or runs SQL; it hands back canned
metadata, and any SQL a tool composes that the test did not anticipate raises
instead of quietly returning nothing. Whether that SQL is valid BigQuery is a
`-m bq` / `-m bq_live` question, and the tests at the bottom of this file are
where it is asked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bpd_mcp.bq import LOGICAL_TABLES, BigQueryWarehouse, LogicalTable
from bpd_mcp.config import Settings
from bpd_mcp.schemas import (
    BigQueryStatusInput,
    DataFreshnessInput,
    DescribeSchemaInput,
    ListDatasetsInput,
)
from bpd_mcp.tools.admin import bigquery_status, data_freshness, list_datasets
from bpd_mcp.tools.query import describe_schema

# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


class FakeClient:
    """Just enough `bigquery.Client` for `bigquery_status` / `_bq_datasets_reachable`."""

    def __init__(
        self, dataset_ids: tuple[str, ...] = (), *, error: Exception | None = None
    ) -> None:
        self._dataset_ids = dataset_ids
        self._error = error

    def list_datasets(self, project: str) -> list[Any]:
        if self._error is not None:
            raise self._error
        return [SimpleNamespace(dataset_id=d, project=project) for d in self._dataset_ids]


class FakeWarehouse:
    """Metadata-only stand-in for `BigQueryWarehouse`'s read surface.

    Every attribute here also exists on the real class — pinned by
    `test_fake_warehouse_surface_exists_on_the_real_warehouse`, so a rename in
    `bq.py` cannot leave these tests passing against an interface nobody has.

    `execute_sql` matches a canned response by substring and RAISES on anything
    unrecognised. A permissive default would let a tool silently change the
    query it runs and still go green.
    """

    def __init__(
        self,
        *,
        registry: dict[str, LogicalTable] | None = None,
        datasets: list[dict[str, Any]] | None = None,
        freshness: dict[str, Any] | None = None,
        row_counts: dict[str, int] | None = None,
        schemas: dict[str, list[tuple[str, str]]] | None = None,
        describe_payload: dict[str, Any] | None = None,
        sql_results: dict[str, tuple[list[str], list[tuple[Any, ...]]]] | None = None,
        client: FakeClient | None = None,
        project: str = "biom-reporting-s26",
        location: str = "us-central1",
        credentials_source: str = "GCP_SA_KEY_B64 env var (materialised to /home/u/.config/gcloud/biom-bq-sa.json)",
        maximum_bytes_billed: int | None = 20 * 1024**3,
    ) -> None:
        self._registry = registry if registry is not None else {}
        self._datasets = datasets or []
        self._freshness = freshness or {
            "per_pattern": [],
            "last_ingest_at": None,
            "total_files": 0,
            "patterns_seen": 0,
        }
        self._row_counts = row_counts or {}
        self._schemas = schemas or {}
        self._describe = describe_payload
        self._sql_results = sql_results or {}
        self._client = client or FakeClient(("biom_canvas", "bpd_raw", "bpd_meta"))
        self.project = project
        self.location = location
        self.credentials_source = credentials_source
        self.maximum_bytes_billed = maximum_bytes_billed
        self.read_only = True
        self.executed: list[str] = []
        self.refreshed = 0

    # --- identity ---
    @property
    def db_path(self) -> str:
        return f"bigquery://{self.project}/{self.location}"

    @property
    def client(self) -> FakeClient:
        return self._client

    @property
    def registry(self) -> dict[str, LogicalTable]:
        return self._registry

    # --- reads ---
    def execute_sql(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        self.executed.append(sql)
        for needle, result in self._sql_results.items():
            if needle in sql:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"FakeWarehouse got unexpected SQL: {sql!r}")

    def list_datasets(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._datasets]

    def freshness_stats(self) -> dict[str, Any]:
        return self._freshness

    def base_row_counts(self) -> dict[str, int]:
        return dict(self._row_counts)

    def logical_schema(self, table: str) -> list[tuple[str, str]]:
        return list(self._schemas[table])

    def describe(self) -> dict[str, Any]:
        if self._describe is None:
            raise AssertionError("this FakeWarehouse was built without a describe payload")
        return self._describe

    def refresh_metadata(self) -> None:
        self.refreshed += 1

    def close(self) -> None:
        pass


def _settings(tmp_path: Path) -> Settings:
    s = Settings(bpd_data_dir=str(tmp_path))
    s.ensure_dirs()
    return s


def _dataset_row(name: str, **over: Any) -> dict[str, Any]:
    """One `warehouse.list_datasets()` row with every key the tools read by name."""
    row: dict[str, Any] = {
        "dataset": name,
        "feed_kind": "append_daily",
        "status": "active",
        "row_count": 1000,
        "date_column": "sales_date",
        "min_date": date(2025, 1, 1),
        "max_date": date(2026, 8, 30),
        "content_column": "sales_date",
        "content_min_date": date(2025, 1, 1),
        "content_max_date": date(2026, 8, 30),
        "file_count": 12,
        "last_loaded_at": datetime(2026, 8, 31, 6, 47, tzinfo=UTC),
    }
    row.update(over)
    return row


def test_fake_warehouse_surface_exists_on_the_real_warehouse() -> None:
    """Every attribute the fake offers must exist on BigQueryWarehouse.

    Without this, renaming (say) `freshness_stats` in bq.py leaves the admin
    tools broken in production and these tests green forever.
    """
    fake = FakeWarehouse()
    public = {
        n
        for n in (*vars(fake), *vars(type(fake)))
        if not n.startswith("_") and n not in {"executed", "refreshed"}
    }
    missing = sorted(n for n in public if not hasattr(BigQueryWarehouse, n))
    assert missing == [], f"FakeWarehouse exposes attributes BigQueryWarehouse does not: {missing}"


# --------------------------------------------------------------------------------------
# bpd_clear_cache / bpd_auth_status / bpd_cache_status are GONE, not renamed shims
# --------------------------------------------------------------------------------------


def test_removed_admin_tools_are_actually_removed() -> None:
    """`clear_cache` is deleted outright; `auth_status`/`cache_status` were renamed.

    A no-op "clear cache" is worse than none — a user who invokes it believes
    state was reset and then reads stale-looking BigQuery results as a failed
    reset — so this guards against someone re-adding a stub for compatibility.
    """
    from bpd_mcp import schemas
    from bpd_mcp.tools import admin

    for gone in ("clear_cache", "cache_status", "auth_status"):
        assert not hasattr(admin, gone), f"tools.admin.{gone} should not exist any more"
    for gone_schema in ("ClearCacheInput", "CacheStatusInput", "AuthStatusInput"):
        assert not hasattr(schemas, gone_schema), f"schemas.{gone_schema} should be gone"


def test_server_registers_the_renamed_admin_tools_only() -> None:
    from bpd_mcp.server import mcp

    names = set(mcp._tool_manager._tools)
    assert {"bpd_bigquery_status", "bpd_data_freshness", "bpd_health_check"} <= names
    assert "bpd_clear_cache" not in names
    assert "bpd_auth_status" not in names
    assert "bpd_cache_status" not in names


# --------------------------------------------------------------------------------------
# bpd_list_datasets
# --------------------------------------------------------------------------------------


async def test_list_datasets_labels_row_counts_as_base_table_counts() -> None:
    """The listing's row_count is the BASE table's, and must say so.

    `orders_daily` reports ~147k base rows against ~7.7k logical rows. Rendering
    that as a bare count in a listing is the same 19x overstatement that
    `describe_schema` had to fix.
    """
    wh = FakeWarehouse(
        datasets=[
            _dataset_row("sales_daily"),
            _dataset_row("orders_daily", feed_kind="delta_latest_state", row_count=147_166),
        ]
    )
    resp = await list_datasets(wh, ListDatasetsInput(response_format="json"))

    assert resp.ok is True
    assert resp.data["row_count_basis"] == "base_table"
    notes = resp.data["notes"]
    assert "OVERSTATES" in notes
    assert "orders_daily" in notes
    assert "bpd_describe_schema" in notes  # points at where the real number lives


async def test_list_datasets_renders_one_row_per_dataset_with_the_documented_columns() -> None:
    """The column list is a contract: `bpd_data_freshness` reads these keys by name."""
    wh = FakeWarehouse(
        datasets=[
            _dataset_row("sales_daily"),
            _dataset_row("sales_weekly", status="active", feed_kind="period_replace"),
            _dataset_row("sales_weekly_item", status="retired", row_count=0),
        ]
    )
    resp = await list_datasets(wh, ListDatasetsInput(response_format="markdown"))

    assert resp.format == "markdown"
    header = resp.rendered.splitlines()[2]
    for col in ("dataset", "feed_kind", "status", "row_count", "min_date", "max_date"):
        assert col in header
    assert [r["dataset"] for r in resp.data["rows"]] == [
        "sales_daily",
        "sales_weekly",
        "sales_weekly_item",
    ]
    assert resp.data["rows"][2]["status"] == "retired"


# --------------------------------------------------------------------------------------
# bpd_bigquery_status  (was bpd_auth_status)
# --------------------------------------------------------------------------------------


async def test_bigquery_status_reports_identity_project_and_no_write_capability() -> None:
    wh = FakeWarehouse(
        registry=dict(LOGICAL_TABLES),
        sql_results={
            "SESSION_USER()": (
                ["session_user"],
                [("bpd-mcp-reader@biom-reporting-s26.iam.gserviceaccount.com",)],
            )
        },
        client=FakeClient(("biom_canvas", "bpd_meta", "bpd_raw")),
    )
    resp = await bigquery_status(wh, BigQueryStatusInput(response_format="json"))

    assert resp.ok is True
    data = resp.data
    assert data["session_user"] == "bpd-mcp-reader@biom-reporting-s26.iam.gserviceaccount.com"
    assert data["project"] == "biom-reporting-s26"
    assert data["location"] == "us-central1"
    assert data["read_only"] is True
    assert "none" in data["write_capability"]
    assert data["datasets_reachable"] == ["biom_canvas", "bpd_meta", "bpd_raw"]
    assert data["logical_tables"] == len(LOGICAL_TABLES)
    assert "session_user_error" not in data
    assert "datasets_reachable_error" not in data


async def test_bigquery_status_never_returns_key_material() -> None:
    """`credentials_source` is provenance, not the credential.

    The GCP_SA_KEY_B64 blob is a full private key; it must not reach a tool
    payload that Claude will print into a transcript.
    """
    secret = "ewogICJwcml2YXRlX2tleSI6ICItLS0tLUJFR0lOIFBSSVZBVEUgS0VZLS0tLS0i"
    wh = FakeWarehouse(
        credentials_source="GCP_SA_KEY_B64 env var (materialised to /home/u/.config/gcloud/biom-bq-sa.json)",
        sql_results={"SESSION_USER()": (["session_user"], [("sa@biom-reporting-s26.iam.gserviceaccount.com",)])},
    )
    resp = await bigquery_status(wh, BigQueryStatusInput(response_format="json"))

    assert "GCP_SA_KEY_B64" in resp.data["credentials_source"]
    assert secret not in resp.rendered
    assert "PRIVATE KEY" not in resp.rendered
    assert "BEGIN" not in resp.rendered


async def test_bigquery_status_degrades_when_the_identity_probe_fails() -> None:
    """One failing probe must not take the whole status report down.

    The point of this tool is diagnosing a broken credential, so it has to still
    answer when the query it runs is exactly what is broken.
    """
    wh = FakeWarehouse(
        sql_results={"SESSION_USER()": PermissionError("403 Access Denied: bigquery.jobs.create")},
        client=FakeClient(("biom_canvas",)),
    )
    resp = await bigquery_status(wh, BigQueryStatusInput(response_format="json"))

    assert resp.ok is True
    assert "session_user" not in resp.data
    assert "PermissionError" in resp.data["session_user_error"]
    assert "403" in resp.data["session_user_error"]
    # ...and the parts that still work are still reported.
    assert resp.data["datasets_reachable"] == ["biom_canvas"]


async def test_bigquery_status_degrades_when_dataset_listing_fails() -> None:
    wh = FakeWarehouse(
        sql_results={"SESSION_USER()": (["session_user"], [("sa@biom-reporting-s26.iam.gserviceaccount.com",)])},
        client=FakeClient(error=RuntimeError("404 Not found: Project biom-reporting-s26")),
    )
    resp = await bigquery_status(wh, BigQueryStatusInput(response_format="json"))

    assert resp.ok is True
    assert resp.data["session_user"].endswith(".iam.gserviceaccount.com")
    assert "RuntimeError" in resp.data["datasets_reachable_error"]
    assert "datasets_reachable" not in resp.data


# --------------------------------------------------------------------------------------
# bpd_data_freshness  (was bpd_cache_status)
# --------------------------------------------------------------------------------------


def _freshness_stats(**over: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "per_pattern": [
            {
                "pattern": "DAILY_SALES_TCIN_LOC",
                "files": 120,
                "max_file_date": date(2026, 8, 30),
                "max_downloaded_at": datetime(2026, 8, 31, 6, 47, tzinfo=UTC),
                "total_bytes": 1234,
                "lag_days": 2,
                "logical_tables": ["sales_daily"],
            }
        ],
        "last_ingest_at": datetime(2026, 8, 31, 6, 47, tzinfo=UTC),
        "total_files": 834,
        "patterns_seen": 18,
    }
    stats.update(over)
    return stats


async def test_data_freshness_business_range_excludes_dimensional_tables(
    tmp_path: Path,
) -> None:
    """The headline range is TRANSACTIONAL only.

    `location_attr.last_remodel_date` reaches back to 2000. Folding it into
    "what data do we have" answered a question nobody asked with a number that
    is wrong by 25 years, so the split is the whole point of the two key pairs.
    """
    wh = FakeWarehouse(
        datasets=[
            _dataset_row("sales_daily", min_date=date(2024, 2, 4), max_date=date(2026, 8, 30)),
            _dataset_row(
                "location_attr",
                feed_kind="dimensional",
                date_column="last_remodel_date",
                min_date=date(2000, 3, 12),
                max_date=date(2026, 8, 1),
                content_min_date=date(2000, 3, 12),
                content_max_date=date(2027, 4, 1),
            ),
        ],
        freshness=_freshness_stats(),
    )
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    assert resp.ok is True
    data = resp.data
    assert data["earliest_data_date"] == date(2024, 2, 4)
    assert data["latest_data_date"] == date(2026, 8, 30)
    assert data["earliest_data_date_including_dimensional"] == date(2000, 3, 12)
    assert data["latest_data_date_including_dimensional"] == date(2026, 8, 30)
    # The dimensional table's forward-reaching remodel dates are not a content
    # horizon for business data either.
    assert data["latest_content_date"] == date(2026, 8, 30)

    by_dataset = {r["dataset"]: r for r in data["per_dataset"]}
    assert by_dataset["sales_daily"]["kind"] == "transactional"
    assert by_dataset["location_attr"]["kind"] == "dimensional"


async def test_data_freshness_reports_the_content_horizon_separately(tmp_path: Path) -> None:
    """Snapshot freshness and content reach are different questions.

    po_plan_daily snapshotted on 2026-07-31 carries order dates two months out;
    reporting only max_date would tell a planner the plan "ends" in July.
    """
    wh = FakeWarehouse(
        datasets=[
            _dataset_row(
                "po_plan_daily",
                feed_kind="accumulating_snapshots",
                date_column="business_d",
                min_date=date(2026, 6, 1),
                max_date=date(2026, 7, 31),
                content_column="order_d",
                content_min_date=date(2026, 6, 2),
                content_max_date=date(2026, 9, 28),
            )
        ],
        freshness=_freshness_stats(),
    )
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    data = resp.data
    assert data["latest_data_date"] == date(2026, 7, 31)
    assert data["latest_content_date"] == date(2026, 9, 28)
    row = {r["dataset"]: r for r in data["per_dataset"]}["po_plan_daily"]
    assert row["feed_kind"] == "accumulating_snapshots"
    assert row["kind"] == "transactional"
    assert row["content_column"] == "order_d"
    assert row["content_max_date"] == date(2026, 9, 28)
    assert row["max_date"] == date(2026, 7, 31)


async def test_data_freshness_reports_the_pipeline_ledger_and_drops_disk_metrics(
    tmp_path: Path,
) -> None:
    """The disk numbers measured a cache that no longer exists; the ledger replaces them."""
    wh = FakeWarehouse(datasets=[_dataset_row("sales_daily")], freshness=_freshness_stats())
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    data = resp.data
    assert data["pipeline_total_files"] == 834
    assert data["pipeline_patterns_seen"] == 18
    assert data["pipeline_last_ingest_at"] == datetime(2026, 8, 31, 6, 47, tzinfo=UTC)
    assert data["per_pattern"][0]["pattern"] == "DAILY_SALES_TCIN_LOC"
    assert data["per_pattern"][0]["logical_tables"] == ["sales_daily"]
    for dead in (
        "raw_dir_bytes",
        "duckdb_file_bytes",
        "ledger_files",
        "ledger_total_bytes",
        "last_sync_finished_at",
        "db_path",
    ):
        assert dead not in data, f"{dead} measured local disk and should be gone"
    # A file landing is not the same fact as rows being queryable, and the tool
    # must say so — this caveat is the only thing standing between
    # "downloaded 20 minutes ago" and "the data is current".
    assert "FILE ARRIVED" in data["caveat"]
    assert "max_date" in data["caveat"]


async def test_data_freshness_handles_a_warehouse_with_nothing_loaded(tmp_path: Path) -> None:
    """No datasets and an empty ledger: null ranges, no crash."""
    wh = FakeWarehouse(datasets=[], freshness=_freshness_stats(per_pattern=[], total_files=0, patterns_seen=0, last_ingest_at=None))
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    assert resp.ok is True
    assert resp.data["earliest_data_date"] is None
    assert resp.data["latest_data_date"] is None
    assert resp.data["latest_content_date"] is None
    assert resp.data["per_dataset"] == []
    assert resp.data["datasets"] == 0


async def test_data_freshness_survives_a_dataset_with_no_date_range(tmp_path: Path) -> None:
    """A logical table whose date sweep returned nothing must not null out the range.

    Under DuckDB this appeared whenever a table existed but held no dates; under
    BigQuery it appears when a feed is retired and its base table is empty.
    """
    wh = FakeWarehouse(
        datasets=[
            _dataset_row("sales_daily", min_date=date(2025, 6, 1), max_date=date(2026, 8, 30)),
            _dataset_row(
                "sales_weekly_item",
                status="retired",
                row_count=0,
                min_date=None,
                max_date=None,
                content_min_date=None,
                content_max_date=None,
            ),
        ],
        freshness=_freshness_stats(),
    )
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    assert resp.data["earliest_data_date"] == date(2025, 6, 1)
    assert resp.data["latest_data_date"] == date(2026, 8, 30)
    retired = {r["dataset"]: r for r in resp.data["per_dataset"]}["sales_weekly_item"]
    assert retired["status"] == "retired"
    assert retired["min_date"] is None


async def test_data_freshness_marks_unknown_datasets_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """A dataset with no DATASET_KINDS entry reports kind='unknown'.

    The drift guard in test_audit_drift_guards.py makes that state unreachable
    in a committed tree; this pins the runtime behaviour if it ever happens.
    """
    wh = FakeWarehouse(datasets=[_dataset_row("shopify_orders_daily")], freshness=_freshness_stats())
    resp = await data_freshness(wh, _settings(tmp_path), DataFreshnessInput(response_format="json"))

    row = resp.data["per_dataset"][0]
    assert row["kind"] == "unknown"
    # An unknown kind is not transactional, so it stays out of the business range.
    assert resp.data["earliest_data_date"] is None


# --------------------------------------------------------------------------------------
# bpd_describe_schema — the row-count basis bug
# --------------------------------------------------------------------------------------


def _describe_payload() -> dict[str, Any]:
    """`describe()`'s real shape, with orders_daily's real 19x overstatement."""
    return {
        "views": [],
        "tables": {
            "sales_daily": {
                "columns": [
                    {"name": "sales_date", "type": "DATE"},
                    {"name": "tcin", "type": "INT64"},
                    {"name": "sale_quantity", "type": "FLOAT64"},
                ],
                "row_count": 489_420,
                "row_count_basis": "base_table",
                "source": "biom-reporting-s26.biom_canvas.fct_target_sales",
                "latest_state_note": None,
            },
            "orders_daily": {
                "columns": [
                    {"name": "snapshot_d", "type": "DATE"},
                    {"name": "purchase_order_id", "type": "INT64"},
                ],
                "row_count": 147_166,
                "row_count_basis": "base_table",
                "source": "biom-reporting-s26.bpd_raw.daily_order_tcin_loc",
                "latest_state_note": (
                    "Reduced to the latest snapshot per (purchase_order_id, tcin, "
                    "receiving_location_id). The base table accumulates every daily "
                    "drop, so its row_count (~147k) is ~19x the logical row count (~7.7k)."
                ),
            },
        },
    }


async def test_describe_schema_markdown_labels_counts_as_base_rows() -> None:
    """The bug this test exists for: `orders_daily (147,166 rows)`.

    describe() reports the PRIMARY BASE TABLE's count, but orders_daily's body
    reduces to latest state — 7,710 logical rows. Printing the base number as
    "rows" states a 19x overstatement as fact in the first tool a user runs, and
    every downstream sanity check ("does 7.7k open-order lines look right?")
    then fails against a number the tool invented.
    """
    wh = FakeWarehouse(describe_payload=_describe_payload())
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="markdown"))

    assert resp.format == "markdown"
    md = resp.rendered
    assert "(147,166 rows)" not in md, "base-table count rendered as if it were the logical count"
    assert "~147,166 base rows" in md
    assert "biom-reporting-s26.bpd_raw.daily_order_tcin_loc" in md
    # The unreduced table is labelled the same way — the basis is a property of
    # how describe() counts, not of which table it counted.
    assert "~489,420 base rows" in md
    assert "(489,420 rows)" not in md


async def test_describe_schema_markdown_surfaces_latest_state_note() -> None:
    """The reduction must be visible to a reader of the rendered markdown.

    `bq.describe()`'s docstring makes this a requirement on the renderer: the
    note is the only place the real magnitude (~7.7k) appears.
    """
    wh = FakeWarehouse(describe_payload=_describe_payload())
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="markdown"))

    md = resp.rendered
    assert "Reduced to the latest snapshot" in md
    assert "19x" in md
    assert "7.7k" in md
    # Rendered as a blockquote attached to its own table's section, not dumped
    # at the end where it reads as a footnote about something else.
    orders_section = md.split("#### `orders_daily`")[1]
    sales_section = md.split("#### `sales_daily`")[1].split("####")[0]
    assert "> Reduced to the latest snapshot" in orders_section
    assert "Reduced to" not in sales_section  # no note on a table that has none


async def test_describe_schema_markdown_lists_columns_and_types() -> None:
    wh = FakeWarehouse(describe_payload=_describe_payload())
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="markdown"))

    md = resp.rendered
    assert "| sales_date | DATE |" in md
    assert "| sale_quantity | FLOAT64 |" in md  # FLOAT64, not INT64: it counts units
    assert "| tcin | INT64 |" in md


async def test_describe_schema_json_carries_the_basis_machine_readably() -> None:
    """JSON callers get the basis as data, not buried in prose."""
    wh = FakeWarehouse(describe_payload=_describe_payload())
    resp = await describe_schema(wh, DescribeSchemaInput(response_format="json"))

    assert resp.format == "json"
    orders = resp.data["tables"]["orders_daily"]
    assert orders["row_count_basis"] == "base_table"
    assert orders["row_count"] == 147_166
    assert orders["source"].endswith("daily_order_tcin_loc")
    assert "19x" in orders["latest_state_note"]


# --------------------------------------------------------------------------------------
# Live BigQuery: the metadata the renderer depends on is really produced
# --------------------------------------------------------------------------------------


@pytest.mark.bq
async def test_describe_reports_base_table_basis_for_a_real_registry_entry(bq_client) -> None:
    """`describe()` over real registry entries, 0 bytes: `__TABLES__` + dry runs.

    This is the other half of the rendering tests above: they prove the renderer
    surfaces `row_count_basis` / `source` / `latest_state_note`, and this proves
    `describe()` still emits them from the live schema. A key rename in bq.py
    would otherwise leave both halves green.
    """
    registry = {name: LOGICAL_TABLES[name] for name in ("sales_daily", "orders_daily")}
    wh = BigQueryWarehouse(client=bq_client, registry=registry)
    info = wh.describe()

    assert set(info["tables"]) == {"sales_daily", "orders_daily"}
    assert info["views"] == []  # BigQuery exposes none; the key must still exist
    orders = info["tables"]["orders_daily"]
    assert orders["row_count_basis"] == "base_table"
    assert orders["source"] == "biom-reporting-s26.bpd_raw.daily_order_tcin_loc"
    assert orders["row_count"] > 0
    assert orders["latest_state_note"] and "19x" in orders["latest_state_note"]
    assert {c["name"] for c in orders["columns"]} >= {"snapshot_d", "purchase_order_id", "tcin"}

    resp = await describe_schema(wh, DescribeSchemaInput(response_format="markdown"))
    assert f"~{orders['row_count']:,} base rows" in resp.rendered
    assert "> Reduced to the latest snapshot" in resp.rendered


@pytest.mark.bq
async def test_bigquery_status_identifies_the_real_service_account(bq_client) -> None:
    """SESSION_USER() answers from the server, so this is the authoritative identity.

    0 bytes: `SELECT SESSION_USER()` scans nothing.
    """
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    resp = await bigquery_status(wh, BigQueryStatusInput(response_format="json"))

    assert resp.ok is True, resp.rendered
    assert "session_user_error" not in resp.data
    who = resp.data["session_user"]
    assert who.endswith("@biom-reporting-s26.iam.gserviceaccount.com"), who
    assert "bpd_raw" in resp.data["datasets_reachable"]
    assert "biom_canvas" in resp.data["datasets_reachable"]
    assert resp.data["logical_tables"] == 15
