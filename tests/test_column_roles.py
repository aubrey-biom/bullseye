"""Column-role resolution against the BigQuery logical-table registry.

Three things changed with the swap, and the tests below are organised around
them:

  * `_columns_of` no longer queries `information_schema`; it calls
    `warehouse.logical_schema(table)`. A CTE-injected logical table appears in
    no catalogue anywhere, so `INFORMATION_SCHEMA` could not answer this
    question even if it were free — which it is not (10 MB minimum per call).
    That makes role resolution *pure* with respect to a stub warehouse, so
    almost everything here runs with no network and no credentials.
  * `table_exists` is registry membership, not a catalogue probe. The registry
    IS the catalogue now; a logical table is always queryable, so unlike the
    DuckDB era (where sync created tables lazily) absence cannot vary at
    runtime.
  * `ResolvedColumn.duckdb_type` is `sql_type`, and `select_as_date` emits
    `SAFE_CAST` with BACKTICK-quoted identifiers. Both halves of that are
    load-bearing and both have a live test below: a plain `CAST` hard-400s the
    entire query on Target's `""` placeholder dates, and a double-quoted
    identifier is a STRING LITERAL in BigQuery rather than a column.
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

import pytest

from bpd_mcp.bq import LOGICAL_TABLES, LogicalTable
from bpd_mcp.column_roles import (
    COLUMN_ROLES,
    DATASET_KINDS,
    ColumnNotFound,
    ResolvedColumn,
    resolve_column,
    table_exists,
)
from tests.conftest import fixture_table

# The real BigQuery types the registry projects, as reported by
# `logical_schema` (which uses the client's legacy field-type spelling:
# INTEGER/FLOAT, not INT64/FLOAT64 — asserting the wrong spelling here would
# make a stub disagree with production).
SALES_DAILY_SCHEMA = [
    ("sales_date", "DATE"),
    ("tcin", "INTEGER"),
    ("location_id", "INTEGER"),
    ("sale_quantity", "FLOAT"),
    ("sale_amount", "FLOAT"),
]
FORECAST_WEEKLY_SCHEMA = [
    ("last_update_d", "DATE"),
    ("tcin", "INTEGER"),
    ("location_id", "INTEGER"),
    ("selected_forecast_q", "FLOAT"),
    ("fiscal_week_begin_d", "DATE"),
]
LOCATION_ATTR_SCHEMA = [
    ("location_number", "INTEGER"),
    # STRING in BigQuery — the DuckDB loader used to coerce it to DATE.
    ("last_remodel_date", "STRING"),
    ("store_open_date", "DATE"),
]


# ---------------------------------------------------------------------------
# resolve_column — pure python against a stub warehouse
# ---------------------------------------------------------------------------


def test_resolve_column_finds_target_real_world_names(stub_warehouse: Any) -> None:
    """Target ships `sale_quantity`, `selected_forecast_q`, `fiscal_week_begin_d`."""
    wh = stub_warehouse(
        {"sales_daily": SALES_DAILY_SCHEMA, "forecast_weekly": FORECAST_WEEKLY_SCHEMA}
    )
    assert resolve_column(wh, "sales_daily", "date").name == "sales_date"
    assert resolve_column(wh, "sales_daily", "units").name == "sale_quantity"
    assert resolve_column(wh, "sales_daily", "dollars").name == "sale_amount"
    assert resolve_column(wh, "sales_daily", "location").name == "location_id"

    assert resolve_column(wh, "forecast_weekly", "date").name == "fiscal_week_begin_d"
    assert resolve_column(wh, "forecast_weekly", "units").name == "selected_forecast_q"
    # The forward-looking split (Patch #12): freshness stamp != content horizon.
    assert resolve_column(wh, "forecast_weekly", "snapshot_date").name == "last_update_d"


def test_resolve_column_carries_the_bigquery_type(stub_warehouse: Any) -> None:
    """`sql_type` (was `duckdb_type`) is what drives the SAFE_CAST decision."""
    wh = stub_warehouse({"sales_daily": SALES_DAILY_SCHEMA})
    col = resolve_column(wh, "sales_daily", "units")
    assert col.sql_type == "FLOAT"
    assert not col.is_date_typed
    assert resolve_column(wh, "sales_daily", "date").sql_type == "DATE"


def test_resolve_column_picks_first_candidate_in_registry_order(stub_warehouse: Any) -> None:
    """`sale_quantity` is first in the `units` list; `units_sold` is a legacy alias."""
    wh = stub_warehouse(
        {"sales_daily": [("tcin", "INTEGER"), ("units_sold", "INTEGER"),
                         ("sale_quantity", "FLOAT")]}
    )
    # Registry order wins over the table's own column order.
    assert resolve_column(wh, "sales_daily", "units").name == "sale_quantity"


def test_resolve_column_is_case_insensitive_but_returns_the_real_name(
    stub_warehouse: Any,
) -> None:
    wh = stub_warehouse({"sales_daily": [("SALE_QUANTITY", "FLOAT")]})
    assert resolve_column(wh, "sales_daily", "units").name == "SALE_QUANTITY"


def test_resolve_column_extra_candidates_are_lowest_priority(stub_warehouse: Any) -> None:
    wh = stub_warehouse(
        {"sales_daily": [("sale_quantity", "FLOAT"), ("my_custom_units", "INTEGER")]}
    )
    # The registry's own candidates still win...
    assert (
        resolve_column(
            wh, "sales_daily", "units", extra_candidates=("my_custom_units",)
        ).name
        == "sale_quantity"
    )
    # ...but an extra candidate is reachable when nothing else matches.
    wh2 = stub_warehouse({"sales_daily": [("my_custom_units", "INTEGER")]})
    assert (
        resolve_column(
            wh2, "sales_daily", "units", extra_candidates=("my_custom_units",)
        ).name
        == "my_custom_units"
    )


def test_resolve_column_missing_raises_with_diagnostic_detail(stub_warehouse: Any) -> None:
    wh = stub_warehouse({"sales_daily": [("tcin", "INTEGER"), ("some_other_col", "STRING")]})
    with pytest.raises(ColumnNotFound) as ei:
        resolve_column(wh, "sales_daily", "units")
    detail = ei.value.detail
    assert detail["dataset"] == "sales_daily"
    assert detail["role"] == "units"
    assert "sale_quantity" in detail["candidates"]
    assert detail["actual_columns"] == ["tcin", "some_other_col"]
    # The message must be self-explanatory in a tool error payload.
    msg = str(ei.value)
    assert "sale_quantity" in msg and "some_other_col" in msg


def test_resolve_column_on_unknown_table_reports_not_found_not_keyerror(
    stub_warehouse: Any,
) -> None:
    """`_columns_of` swallows the registry KeyError so the caller sees the rich error.

    A bare KeyError here surfaces to the MCP client as an unhandled exception
    instead of a SCHEMA_INCOMPATIBLE tool response.
    """
    wh = stub_warehouse({"sales_daily": SALES_DAILY_SCHEMA})
    with pytest.raises(ColumnNotFound) as ei:
        resolve_column(wh, "no_such_table", "date")
    assert ei.value.detail["actual_columns"] == []


# ---------------------------------------------------------------------------
# table_exists — registry membership
# ---------------------------------------------------------------------------


def test_table_exists_is_registry_membership(stub_warehouse: Any) -> None:
    wh = stub_warehouse({"sales_daily": SALES_DAILY_SCHEMA})
    assert table_exists(wh, "sales_daily") is True
    assert table_exists(wh, "sales_hourly") is False


def test_table_exists_never_queries_the_schema() -> None:
    """0 bytes and no round trip: membership only, even for a known table."""

    class _NoSchemaWarehouse:
        registry: ClassVar[dict[str, Any]] = {"sales_daily": None}

        def logical_schema(self, table: str) -> list[tuple[str, str]]:
            raise AssertionError("table_exists must not read the schema")

    assert table_exists(_NoSchemaWarehouse(), "sales_daily") is True


def test_table_exists_is_false_for_a_registryless_object() -> None:
    """Guards the `except TypeError` branch — a None registry must not explode."""

    class _Broken:
        registry = None

    assert table_exists(_Broken(), "sales_daily") is False


def test_every_registry_table_exists_against_the_real_registry() -> None:
    """No credential needed: LOGICAL_TABLES is a module-level dict."""

    class _RealRegistry:
        registry = LOGICAL_TABLES

    wh = _RealRegistry()
    for name in LOGICAL_TABLES:
        assert table_exists(wh, name) is True
    assert table_exists(wh, "sales_daily_v2") is False


# ---------------------------------------------------------------------------
# ResolvedColumn.select_as_date — the SAFE_CAST / backtick contract
# ---------------------------------------------------------------------------


def test_select_as_date_passes_through_a_date_typed_column() -> None:
    rc = ResolvedColumn(name="sales_date", sql_type="DATE")
    assert rc.is_date_typed
    assert rc.select_as_date() == "`sales_date`"


@pytest.mark.parametrize("sql_type", ["DATE", "DATETIME", "TIMESTAMP"])
def test_is_date_typed_covers_every_temporal_type(sql_type: str) -> None:
    assert ResolvedColumn(name="d", sql_type=sql_type).is_date_typed


@pytest.mark.parametrize("sql_type", ["STRING", "INTEGER", "FLOAT", "NUMERIC", "BOOLEAN"])
def test_is_date_typed_is_false_for_non_temporal_types(sql_type: str) -> None:
    assert not ResolvedColumn(name="d", sql_type=sql_type).is_date_typed


def test_select_as_date_emits_safe_cast_and_backticks_for_a_string_column() -> None:
    """The single highest-value assertion in this file.

    Two independent production failures are pinned here:

      1. `CAST`, not `SAFE_CAST`, aborts the WHOLE query with
         `400 Invalid date: '""'` the moment one row carries Target's `""`
         placeholder — and `location_attr.last_remodel_date` is a STRING full
         of them. One bad row would take down every date-ranged tool.
      2. A DOUBLE-quoted identifier is a string literal in BigQuery, not a
         column: `CAST("last_remodel_date" AS DATE)` would either error or,
         worse, silently become a constant.
    """
    rc = ResolvedColumn(name="last_remodel_date", sql_type="STRING")
    expr = rc.select_as_date()
    assert expr == "SAFE_CAST(`last_remodel_date` AS DATE)"
    # Spelled out so a regression to plain CAST cannot pass on a substring match.
    assert expr.startswith("SAFE_CAST(")
    assert '"' not in expr


def test_location_attr_date_role_resolves_to_a_string_and_gets_safe_cast(
    stub_warehouse: Any,
) -> None:
    """The end-to-end shape of the bug, resolved through the real role registry.

    `COLUMN_ROLES["location_attr"]["date"]` points at `last_remodel_date`, which
    BigQuery keeps as STRING (DuckDB's CSV loader used to coerce it to DATE).
    574 of its 2,222 production rows hold Target's `""` placeholder, so the
    expression built from this resolution MUST be a SAFE_CAST.
    """
    wh = stub_warehouse({"location_attr": LOCATION_ATTR_SCHEMA})
    col = resolve_column(wh, "location_attr", "date")
    assert col.name == "last_remodel_date"
    assert col.sql_type == "STRING"
    assert col.select_as_date(alias="d") == "SAFE_CAST(`last_remodel_date` AS DATE) AS `d`"
    # The location role is `location_number` here, not `location_id`.
    assert resolve_column(wh, "location_attr", "location").name == "location_number"


def test_select_as_date_backticks_the_alias_too() -> None:
    typed = ResolvedColumn(name="sales_date", sql_type="DATE").select_as_date(alias="d")
    assert typed == "`sales_date` AS `d`"
    untyped = ResolvedColumn(name="fiscal_week_begin_d", sql_type="STRING").select_as_date(
        alias="d"
    )
    assert untyped == "SAFE_CAST(`fiscal_week_begin_d` AS DATE) AS `d`"


# ---------------------------------------------------------------------------
# Registry <-> COLUMN_ROLES drift guards (pure python)
# ---------------------------------------------------------------------------


def test_column_roles_covers_every_logical_table() -> None:
    """LOGICAL_TABLES replaced parsers.PATTERNS as the dataset catalogue.

    A logical table with no COLUMN_ROLES entry resolves nothing, so every
    analytics tool that touches it returns SCHEMA_INCOMPATIBLE.
    """
    assert set(COLUMN_ROLES) == set(LOGICAL_TABLES), (
        f"in registry only: {set(LOGICAL_TABLES) - set(COLUMN_ROLES)}; "
        f"in COLUMN_ROLES only: {set(COLUMN_ROLES) - set(LOGICAL_TABLES)}"
    )


def test_dataset_kinds_classifies_every_logical_table() -> None:
    assert set(DATASET_KINDS) == set(LOGICAL_TABLES)
    assert set(DATASET_KINDS.values()) == {"transactional", "dimensional"}
    for ds in ("location_attr", "item_attr", "item_attr_extended"):
        assert DATASET_KINDS[ds] == "dimensional"
    for ds in ("sales_daily", "sales_weekly", "inventory_daily", "forecast_weekly",
               "orders_daily", "po_plan_daily"):
        assert DATASET_KINDS[ds] == "transactional"


def test_required_roles_reference_declared_candidate_lists() -> None:
    from bpd_mcp.column_roles import REQUIRED_ROLES

    for ds, roles in REQUIRED_ROLES.items():
        assert ds in COLUMN_ROLES, f"REQUIRED_ROLES references unknown dataset {ds}"
        for role in roles:
            assert role in COLUMN_ROLES[ds], (
                f"REQUIRED_ROLES demands {ds}.{role} but the registry has no "
                "candidate list for it"
            )


def test_feed_kinds_and_date_range_roles_complete_and_consistent() -> None:
    from bpd_mcp.column_roles import DATE_RANGE_ROLES, FEED_KINDS

    assert set(FEED_KINDS) == set(LOGICAL_TABLES)
    allowed = {
        "delta_latest_state", "accumulating_snapshots", "period_replace",
        "append_daily", "keyed_overwrite_mixed", "dimensional",
    }
    assert set(FEED_KINDS.values()) <= allowed
    for ds, roles_map in DATE_RANGE_ROLES.items():
        assert ds in LOGICAL_TABLES
        assert set(roles_map) == {"snapshot", "content"}
        for role in roles_map.values():
            assert role in COLUMN_ROLES[ds], (
                f"DATE_RANGE_ROLES demands {ds}.{role} but the registry has "
                "no candidate list for it"
            )


def test_known_unpopulated_columns_never_in_candidate_lists() -> None:
    """No tool may resolve (and then filter on) a column Target never populates.

    Also guards the Patch-#10 requirement that query.py's parallel candidate
    tuples stay deleted.
    """
    from bpd_mcp import column_roles as cr
    from bpd_mcp.tools import query as query_mod

    banned = {col for cols in cr.KNOWN_UNPOPULATED_AT_SOURCE.values() for col in cols}
    assert banned, "the guard is vacuous if nothing is listed"
    for ds, roles in cr.COLUMN_ROLES.items():
        for role, candidates in roles.items():
            hits = banned.intersection(candidates)
            assert not hits, f"{ds}.{role} lists known-unpopulated column(s) {hits}"
    for stale in (
        "_QTY_COL_CANDIDATES",
        "_DATE_COL_CANDIDATES",
        "_LOC_COL_CANDIDATES",
        "_STATUS_COL_CANDIDATES",
        "_first_present",
    ):
        assert not hasattr(query_mod, stale), (
            f"tools/query.py regrew {stale} — route through column_roles instead"
        )


# ---------------------------------------------------------------------------
# validate_roles — pure python
# ---------------------------------------------------------------------------


class _RoleWarehouse:
    """Registry + schemas + base-table row counts, with no BigQuery behind it.

    `StubWarehouse` is not enough for `validate_roles`: the emptiness probe
    reads `registry[ds].primary_base_table` and looks it up in
    `base_row_counts()`, so the registry values must be real `LogicalTable`s.
    """

    def __init__(
        self,
        schemas: dict[str, list[tuple[str, str]]],
        row_counts: dict[str, int] | None = None,
    ) -> None:
        self._schemas = schemas
        self.registry = {
            name: LogicalTable(
                name=name,
                sql=f"SELECT 1 -- {name}",
                base_tables=(f"proj.ds.{name}_base",),
                date_column=cols[0][0],
            )
            for name, cols in schemas.items()
        }
        self._counts = (
            {f"proj.ds.{n}_base": 1 for n in schemas}
            if row_counts is None
            else row_counts
        )

    def logical_schema(self, table: str) -> list[tuple[str, str]]:
        return self._schemas[table]

    def base_row_counts(self) -> dict[str, int]:
        return dict(self._counts)


def test_validate_roles_passes_when_every_required_role_resolves() -> None:
    from bpd_mcp.column_roles import validate_roles

    wh = _RoleWarehouse(
        {"sales_daily": SALES_DAILY_SCHEMA, "forecast_weekly": FORECAST_WEEKLY_SCHEMA}
    )
    assert validate_roles(wh) == []


def test_validate_roles_reports_unresolvable_required_roles() -> None:
    from bpd_mcp.column_roles import validate_roles

    wh = _RoleWarehouse({"inventory_daily": [("nothing_useful", "STRING")]})
    failures = validate_roles(wh)
    assert {(f["dataset"], f["role"]) for f in failures} == {
        ("inventory_daily", "date"),
        ("inventory_daily", "on_hand"),
        ("inventory_daily", "tcin"),
        ("inventory_daily", "location"),
    }
    assert all(f["required"] is True for f in failures)
    # The diagnostic detail rides along so the health check can print it.
    one = next(f for f in failures if f["role"] == "on_hand")
    assert "ending_on_hand_q" in one["candidates"]
    assert one["actual_columns"] == ["nothing_useful"]


def test_validate_roles_skips_absent_and_empty_tables() -> None:
    from bpd_mcp.column_roles import validate_roles

    # Nothing in the registry at all.
    assert validate_roles(_RoleWarehouse({})) == []
    # Present, hopeless columns, but the base table has zero rows: skipped,
    # because an empty table's schema says nothing about role drift.
    empty = _RoleWarehouse(
        {"inventory_daily": [("nothing_useful", "STRING")]},
        row_counts={"proj.ds.inventory_daily_base": 0},
    )
    assert validate_roles(empty) == []


def test_validate_roles_survives_a_logical_table_with_no_base_tables() -> None:
    """A `base_tables=()` entry must be SKIPPED, not raise.

    The emptiness probe reads `LogicalTable.primary_base_table`, i.e.
    `base_tables[0]`. `bq.py`'s extension seam documents composing a logical
    table purely out of other logical names via `depends_on` — such a body
    reads no base table directly, so `base_tables=()` is legitimate — and
    conftest's `fixture_table()` builds exactly that shape. Before the fix it
    raised `IndexError: tuple index out of range`.

    Nothing crashed on that: `build_context` wraps its startup pass in
    `except Exception`, and the `roles_resolvable` health check is wrapped by
    `_timed`. The damage is that both are all-or-nothing — the exception
    escapes the loop, so rewriting ONE demanded table to compose from other
    logical names silently stops EVERY dataset from being validated (measured:
    `roles_resolvable` answers "check raised: IndexError: tuple index out of
    range"), trading the drift warnings the pass exists for against a single
    `role_validation_failed` log line and a health check that answers "check
    raised: IndexError". The surrounding `except` already meant to answer
    "cannot tell -> False"; IndexError is the same case.

    Reverting the `IndexError` in `column_roles._dataset_has_rows` makes this
    error rather than fail, which is the point.
    """
    from bpd_mcp.column_roles import _dataset_has_rows, validate_roles

    composed = fixture_table(
        "inventory_daily",
        [{"nothing_useful": "x"}],
        date_column="nothing_useful",
    )
    assert composed.base_tables == (), "fixture_table is the shape under test"

    class _Composed:
        registry: ClassVar[dict[str, Any]] = {"inventory_daily": composed}

        def logical_schema(self, table: str) -> list[tuple[str, str]]:
            return [("nothing_useful", "STRING")]

        def base_row_counts(self) -> dict[str, int]:
            return {}

    wh = _Composed()
    # Skipped as "no rows I can count", exactly like a zero-row base table...
    assert _dataset_has_rows(wh, "inventory_daily") is False
    # ...so validate_roles reports nothing instead of raising, even though the
    # only column present resolves none of inventory_daily's required roles.
    assert validate_roles(wh) == []


def test_validate_roles_flags_date_range_roles_as_soft() -> None:
    """Patch #12 pin: a DATE_RANGE_ROLES-only role failure is `required=False`.

    Its consumers (the dataset listings) degrade to single-date reporting
    rather than erroring, so a hard FAIL would claim "analytics tools WILL
    fail" when none would. Deleting the DATE_RANGE_ROLES merge from
    `validate_roles` makes this test fail.
    """
    from bpd_mcp.column_roles import validate_roles

    # forecast_weekly WITHOUT any snapshot_date candidate: date/units/tcin
    # (REQUIRED_ROLES) all resolve; snapshot_date (DATE_RANGE_ROLES only) cannot.
    wh = _RoleWarehouse(
        {
            "forecast_weekly": [
                ("tcin", "INTEGER"),
                ("location_id", "INTEGER"),
                ("fiscal_week_begin_d", "DATE"),
                ("selected_forecast_q", "FLOAT"),
            ]
        }
    )
    failures = validate_roles(wh)
    assert [(f["dataset"], f["role"], f["required"]) for f in failures] == [
        ("forecast_weekly", "snapshot_date", False)
    ]


# ---------------------------------------------------------------------------
# Live BigQuery: the registry's REAL projected schemas (0 bytes)
# ---------------------------------------------------------------------------


@pytest.mark.bq
def test_required_roles_resolve_against_the_live_registry(bq_client: Any) -> None:
    """The executable form of "the candidate lists match what production ships".

    Every `logical_schema` call is a dry run of `SELECT * FROM (<body>) LIMIT 0`
    — 0 bytes billed — so this reads the REAL projected columns of all 15
    logical tables without scanning a row. If a source column is renamed and
    COLUMN_ROLES is not updated, this fails here instead of one analytics tool
    at a time in production.
    """
    from bpd_mcp.bq import BigQueryWarehouse
    from bpd_mcp.column_roles import REQUIRED_ROLES, validate_roles

    wh = BigQueryWarehouse(client=bq_client)
    assert validate_roles(wh) == []

    # Spot-check that the roles land on the REAL Target columns rather than on
    # a legacy alias that happens to also be present.
    expected = {
        ("sales_daily", "date"): "sales_date",
        ("sales_daily", "units"): "sale_quantity",
        ("inventory_daily", "date"): "business_d",
        ("inventory_daily", "on_hand"): "ending_on_hand_q",
        ("orders_daily", "ordered"): "revised_order_q",
        ("orders_daily", "received"): "item_received_q",
        ("orders_daily", "cancel_remaining"): "cancel_remaining_order_q",
        ("orders_daily", "location"): "receiving_location_id",
        ("po_plan_daily", "units"): "ordered_q",
        ("po_plan_daily", "order_date"): "order_d",
        ("forecast_weekly", "units"): "selected_forecast_q",
        ("forecast_weekly", "snapshot_date"): "last_update_d",
        ("gross_margin", "date"): "fiscal_week_end_d",
    }
    for (ds, role), name in expected.items():
        assert resolve_column(wh, ds, role).name == name, f"{ds}.{role}"
    # Every dataset named above is one validate_roles actually checked.
    assert {ds for ds, _ in expected} <= set(REQUIRED_ROLES)


@pytest.mark.bq
def test_location_attr_date_is_a_string_in_bigquery(bq_client: Any) -> None:
    """Why SAFE_CAST is mandatory, established against the live schema.

    DuckDB's CSV loader coerced `Last Remodel Date` to a DATE column; BigQuery
    keeps the raw STRING. So the `date` role for location_attr resolves to a
    STRING and every date expression built from it goes through SAFE_CAST.
    """
    from bpd_mcp.bq import BigQueryWarehouse

    wh = BigQueryWarehouse(client=bq_client)
    col = resolve_column(wh, "location_attr", "date")
    assert col.name == "last_remodel_date"
    assert col.sql_type == "STRING"
    assert not col.is_date_typed
    assert col.select_as_date() == "SAFE_CAST(`last_remodel_date` AS DATE)"


@pytest.mark.bq
def test_plain_cast_would_break_on_targets_placeholder_but_safe_cast_does_not(
    fixture_warehouse: Any,
) -> None:
    """Runs both spellings on real BigQuery over a literal `""` row (0 bytes).

    This is the regression that motivated the SAFE_CAST rule, reproduced end to
    end: the plain CAST does not skip the bad row, it fails the whole query.
    """
    from google.api_core import exceptions as gexc

    wh = fixture_warehouse(
        location_attr=[
            {"location_number": 1, "last_remodel_date": "2026-04-13"},
            # Target's placeholder for "no value": the two-character string `""`.
            {"location_number": 2, "last_remodel_date": '""'},
        ]
    )
    col = ResolvedColumn(name="last_remodel_date", sql_type="STRING")
    assert col.select_as_date() == "SAFE_CAST(`last_remodel_date` AS DATE)"

    _cols, rows = wh.execute_sql(
        f"SELECT {col.select_as_date(alias='d')} FROM location_attr ORDER BY location_number"
    )
    assert [r[0] for r in rows] == [date(2026, 4, 13), None]

    with pytest.raises(gexc.BadRequest) as ei:
        wh.execute_sql(
            "SELECT CAST(`last_remodel_date` AS DATE) AS d FROM location_attr"
        )
    assert "Invalid date" in str(ei.value)
