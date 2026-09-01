"""Shared pytest fixtures and path setup.

Three tiers, because the swap to BigQuery removed the option of "just run the
SQL against a local engine":

  (default)  Pure-python. No network, no credentials, no cost. CTE injection,
             dialect helpers, the registry, SQL safety, role resolution against
             a stub. This is what runs on every `pytest`.
  -m bq      Executes real SQL on BigQuery, but every logical table is swapped
             for a literal `SELECT ... UNION ALL SELECT ...` fixture body, so
             BigQuery scans no table: 0 bytes billed, ~0.7s per query. This is
             how we test that the ANALYTICS SQL IS VALID BIGQUERY and computes
             the right answer over known rows.
  -m bq_live Executes against real production tables. Bills bytes. Reserved for
             a handful of shape/freshness assertions.

Why there is no DuckDB test double: it was measured and rejected. DuckDB cannot
parse SAFE_CAST, backtick identifiers, or `DATE_TRUNC(x, WEEK(MONDAY))`, so most
ported SQL will not even run. Worse, where both engines DO accept the same text
they disagree: `SELECT "sale_quantity"` returns the column in DuckDB and the
constant STRING 'sale_quantity' in BigQuery. A DuckDB double would therefore go
green exactly when production is broken — the one failure mode a test suite
must never have. Fixture CTEs cost the same (nothing) and run on the real
engine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make `src/` importable without an editable install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# Prevent a .env in the user's home from leaking into tests.
os.environ.setdefault("BPD_DATA_DIR", str(ROOT / ".test-data"))


# ---------------------------------------------------------------------------
# Credential gating
# ---------------------------------------------------------------------------


def bigquery_available() -> bool:
    """Can this environment reach BigQuery at all?

    Checked WITHOUT importing google.cloud or touching the network, so the
    default tier stays fast and dependency-light.
    """
    return bool(os.environ.get("GCP_SA_KEY_B64") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip the BigQuery tiers when there is no credential, rather than erroring.

    A contributor without warehouse access still gets a meaningful green run
    from the default tier; CI with a key gets everything.
    """
    if bigquery_available():
        return
    skip = pytest.mark.skip(reason="no BigQuery credential (GCP_SA_KEY_B64 / ADC) in this env")
    for item in items:
        if "bq" in item.keywords or "bq_live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Fixture logical tables
# ---------------------------------------------------------------------------

# BigQuery infers a literal's type from its form, and the inferred type is what
# `logical_schema()` reports and therefore what `resolve_column` hands to
# `select_as_date`. Pin the production types explicitly so a fixture cannot
# accidentally test a type production never produces. Note sale_quantity is
# FLOAT64 in production even though it counts units.
#
# Two spellings appear for a couple of these: the RAW source column and the name
# the registry body projects it under. `bq.py` aliases `inventory_date AS
# business_d` and `fiscal_week_end_date AS fiscal_week_end_d`, and both spellings
# are live COLUMN_ROLES candidates, so both are pinned — a fixture may legitimately
# stand in for either side of the alias.
_TYPES: dict[str, str] = {
    "sales_date": "DATE",
    "inventory_date": "DATE",  # raw; projected as business_d
    "business_d": "DATE",
    "snapshot_d": "DATE",
    "fiscal_week_begin_d": "DATE",
    "fiscal_week_end_date": "DATE",  # raw; projected as fiscal_week_end_d
    "fiscal_week_end_d": "DATE",
    "last_update_d": "DATE",
    "processed_ct_date": "DATE",
    "order_d": "DATE",
    "original_estimated_arrival_d": "DATE",
    "revised_estimated_arrival_d": "DATE",
    "purchase_order_create_d": "DATE",
    "tcin": "INT64",
    "location_id": "INT64",
    "receiving_location_id": "INT64",
    "purchase_order_id": "INT64",
    "sale_quantity": "FLOAT64",
    "sale_amount": "FLOAT64",
    "ending_on_hand_q": "INT64",
    "selected_forecast_q": "FLOAT64",
    "ordered_q": "INT64",
    "revised_order_q": "INT64",
    "item_received_q": "FLOAT64",
    "cancel_remaining_order_q": "INT64",
}


def _literal(col: str, value: Any) -> str:
    """Render one Python value as a typed BigQuery literal."""
    sql_type = _TYPES.get(col, "STRING" if isinstance(value, str) else "INT64")
    if value is None:
        return f"CAST(NULL AS {sql_type})"
    if sql_type == "DATE":
        return f"DATE '{value}'"
    if sql_type == "STRING":
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return f"CAST({value} AS {sql_type})"


def fixture_table(
    name: str,
    rows: list[dict[str, Any]],
    *,
    date_column: str | None = None,
    columns: list[str] | None = None,
) -> Any:
    """Build a `LogicalTable` whose body is literal rows — no table is scanned.

    `rows` must be non-empty and uniformly keyed; `columns` pins projection
    order when dict order is not what you want. The first SELECT carries the
    `AS col` aliases and the casts that fix each column's type; the rest are
    bare SELECTs, which is both cheaper to read and how BigQuery wants it.
    """
    from bpd_mcp.bq import LogicalTable

    if not rows:
        raise ValueError(f"fixture_table({name!r}) needs at least one row to infer types")
    cols = columns or list(rows[0])
    selects = []
    for i, row in enumerate(rows):
        cells = [
            f"{_literal(c, row.get(c))}" + (f" AS {c}" if i == 0 else "") for c in cols
        ]
        selects.append("SELECT " + ", ".join(cells))
    return LogicalTable(
        name=name,
        sql="\n UNION ALL ".join(selects),
        base_tables=(),
        date_column=date_column or cols[0],
        patterns=(),
    )


@pytest.fixture(scope="session")
def bq_client() -> Any:
    """One real BigQuery client for the whole session."""
    from bpd_mcp.bq import BQ_LOCATION_DEFAULT, BQ_PROJECT_DEFAULT, resolve_credentials

    # Materialise the service-account key before the client library is imported or
    # constructed, so GOOGLE_APPLICATION_CREDENTIALS is already pointing at it.
    resolve_credentials()

    from google.cloud import bigquery

    return bigquery.Client(project=BQ_PROJECT_DEFAULT, location=BQ_LOCATION_DEFAULT)


@pytest.fixture
def fixture_warehouse(bq_client: Any) -> Any:
    """Factory: `fixture_warehouse(sales_daily=[{...}, ...])` -> BigQueryWarehouse.

    The returned warehouse runs real BigQuery SQL against literal rows, so the
    analytics tools are exercised end to end — dialect, CTE injection, role
    resolution and aggregation — while scanning nothing.
    """
    from bpd_mcp.bq import BigQueryWarehouse

    def _make(**tables: Any) -> BigQueryWarehouse:
        registry = {}
        for tname, spec in tables.items():
            registry[tname] = spec if hasattr(spec, "sql") else fixture_table(tname, spec)
        return BigQueryWarehouse(client=bq_client, registry=registry)

    return _make


class StubWarehouse:
    """Minimal warehouse for pure-python tests of role resolution.

    Implements only what `column_roles` reads: `registry` membership and
    `logical_schema`. No network, no credentials.
    """

    def __init__(self, schemas: dict[str, list[tuple[str, str]]]) -> None:
        self._schemas = schemas

    @property
    def registry(self) -> dict[str, Any]:
        return dict.fromkeys(self._schemas)

    def logical_schema(self, table: str) -> list[tuple[str, str]]:
        return self._schemas[table]

    def base_row_counts(self) -> dict[str, int]:
        return {}


@pytest.fixture
def stub_warehouse() -> Any:
    return StubWarehouse
