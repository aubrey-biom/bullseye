"""Static drift guards: the parallel sources of truth that must agree.

`bq.py`'s extension seam says adding a logical table is a data change, and lists
four things you must also do elsewhere — COLUMN_ROLES, DATASET_KINDS,
FEED_KINDS, schemas.KnownDataset — closing with "a drift guard asserts these key
sets are identical; a missing entry fails the suite." That guard did not exist,
which made the seam's safety net imaginary: a new registry entry would sail
through review and then fail at runtime, one tool at a time, with
`ColumnNotFound` / `kind='unknown'` / `feed_kind='unknown'` / an MCP argument
rejected by a Literal that never heard of it.

This file is that net. Everything here is pure python and free; the `-m bq`
tests at the bottom check the same declarations against the live BigQuery
schema, which is what stops the free guards from being satisfied by a parser
that agrees with itself.
"""

from __future__ import annotations

import re
import typing

import pytest

from bpd_mcp import column_roles as cr
from bpd_mcp import schemas
from bpd_mcp.bq import (
    KNOWN_DATASET_NAMES,
    LOGICAL_TABLES,
    BigQueryWarehouse,
    base_datasets,
    heuristic_date_column,
)
from bpd_mcp.tools.admin import EXPECTED_DATA_GRAINS, EXPECTED_TOOL_COUNT

REGISTRY = frozenset(LOGICAL_TABLES)


def _literal_values(t) -> set[str]:
    """The set of string values in a `Literal[...]` type alias."""
    return set(typing.get_args(t))


def _diff(name: str, other: set[str]) -> str:
    return (
        f"drift between bq.LOGICAL_TABLES and {name}:\n"
        f"  in LOGICAL_TABLES only: {sorted(REGISTRY - other)}\n"
        f"  in {name} only:         {sorted(other - REGISTRY)}"
    )


# --------------------------------------------------------------------------------------
# The five key sets the extension seam promises are identical
# --------------------------------------------------------------------------------------


def test_column_roles_covers_exactly_the_registry() -> None:
    """Missing entry -> `resolve_column` raises and every analytics tool on that
    table returns SCHEMA_INCOMPATIBLE."""
    other = set(cr.COLUMN_ROLES)
    assert other == REGISTRY, _diff("column_roles.COLUMN_ROLES", other)


def test_dataset_kinds_covers_exactly_the_registry() -> None:
    """Missing entry -> `bpd_data_freshness` reports kind='unknown', and the
    table silently drops out of the transactional business-date range."""
    other = set(cr.DATASET_KINDS)
    assert other == REGISTRY, _diff("column_roles.DATASET_KINDS", other)


def test_feed_kinds_covers_exactly_the_registry() -> None:
    """Missing entry -> `bpd_list_datasets` reports feed_kind='unknown', which is
    the field that tells a caller whether to filter to one business_d."""
    other = set(cr.FEED_KINDS)
    assert other == REGISTRY, _diff("column_roles.FEED_KINDS", other)


def test_known_dataset_literal_covers_exactly_the_registry() -> None:
    """Missing entry -> MCP argument validation rejects the new dataset name.

    The Literal is spelled out by hand (so clients get a real enum in the
    published tool schema) rather than generated, which is precisely why it
    needs pinning.
    """
    other = _literal_values(schemas.KnownDataset)
    assert other == REGISTRY, _diff("schemas.KnownDataset", other)


def test_known_dataset_names_is_the_registry_in_registry_order() -> None:
    assert tuple(LOGICAL_TABLES) == KNOWN_DATASET_NAMES
    assert len(KNOWN_DATASET_NAMES) == len(set(KNOWN_DATASET_NAMES))


def test_satellite_role_maps_reference_only_real_logical_tables() -> None:
    """REQUIRED_ROLES / DATE_RANGE_ROLES / KNOWN_UNPOPULATED_AT_SOURCE are keyed
    by logical table too. A stale key here is dead weight that never fires —
    `validate_roles` skips tables that are not in the registry."""
    for name, mapping in (
        ("REQUIRED_ROLES", cr.REQUIRED_ROLES),
        ("DATE_RANGE_ROLES", cr.DATE_RANGE_ROLES),
        ("KNOWN_UNPOPULATED_AT_SOURCE", cr.KNOWN_UNPOPULATED_AT_SOURCE),
    ):
        unknown = sorted(set(mapping) - REGISTRY)
        assert unknown == [], f"column_roles.{name} keys not in the registry: {unknown}"


def test_every_demanded_role_exists_in_column_roles() -> None:
    """A role demanded by REQUIRED_ROLES/DATE_RANGE_ROLES but absent from
    COLUMN_ROLES has an EMPTY candidate list, so it can never resolve — the
    health check would report an unfixable failure with `tried []`."""
    problems: list[str] = []
    for dataset, roles in cr.REQUIRED_ROLES.items():
        for role in roles:
            if not cr.COLUMN_ROLES.get(dataset, {}).get(role):
                problems.append(f"REQUIRED_ROLES[{dataset!r}] demands {role!r}")
    for dataset, mapping in cr.DATE_RANGE_ROLES.items():
        for kind, role in mapping.items():
            if not cr.COLUMN_ROLES.get(dataset, {}).get(role):
                problems.append(f"DATE_RANGE_ROLES[{dataset!r}][{kind!r}] demands {role!r}")
    assert problems == [], "roles with no candidate list: " + "; ".join(problems)


def test_known_unpopulated_columns_are_not_candidates_for_any_role() -> None:
    """These columns are Target's `""` placeholder in ~98% of rows. Letting one
    become a role candidate means a tool resolves to it and filters the order
    book down to nothing."""
    offenders = [
        f"{dataset}.{col} is a candidate for role {role!r}"
        for dataset, cols in cr.KNOWN_UNPOPULATED_AT_SOURCE.items()
        for col in cols
        for role, candidates in cr.COLUMN_ROLES.get(dataset, {}).items()
        if col in candidates
    ]
    assert offenders == [], "; ".join(offenders)


def test_dataset_and_feed_kind_values_stay_in_their_documented_vocabularies() -> None:
    assert set(cr.DATASET_KINDS.values()) <= {"transactional", "dimensional"}
    assert set(cr.FEED_KINDS.values()) <= {
        "delta_latest_state",
        "accumulating_snapshots",
        "period_replace",
        "append_daily",
        "keyed_overwrite_mixed",
        "dimensional",
    }


# --------------------------------------------------------------------------------------
# Registry shape
# --------------------------------------------------------------------------------------


def test_every_registry_entry_names_itself_and_a_fully_qualified_source() -> None:
    for name, entry in LOGICAL_TABLES.items():
        assert entry.name == name, f"registry key {name!r} != LogicalTable.name {entry.name!r}"
        assert entry.base_tables, f"{name} declares no base_tables; primary_base_table would raise"
        for fq in entry.base_tables:
            assert fq.count(".") == 2, f"{name} base table {fq!r} is not project.dataset.table"
            assert fq.startswith("biom-reporting-s26."), f"{name} reads outside the project: {fq}"
    assert base_datasets() == frozenset({"biom_canvas", "bpd_raw"})


def test_registry_dependencies_reference_known_tables() -> None:
    """`depends_on` composes logical tables; a typo would surface as BigQuery's
    `Unrecognized name`, at query time, in whichever tool referenced it."""
    for name, entry in LOGICAL_TABLES.items():
        unknown = sorted(set(entry.depends_on) - REGISTRY)
        assert unknown == [], f"{name}.depends_on references unknown table(s): {unknown}"
        assert name not in entry.depends_on, f"{name}.depends_on includes itself"


def test_data_grain_literals_in_the_registry_are_all_expected() -> None:
    """`sales_weekly` selects `data_grain IN ('weekly','history_weekly')`, and the
    `registry_tables_resolve` health check warns when biom_canvas ships a value
    outside EXPECTED_DATA_GRAINS. If a body starts filtering on a fourth value,
    the guard's list has to learn about it in the same commit — otherwise the
    check warns about a value we deliberately use."""
    used: set[str] = set()
    for entry in LOGICAL_TABLES.values():
        for match in re.finditer(r"data_grain\s*(?:=|IN)\s*(?:\(([^)]*)\)|'([^']*)')", entry.sql):
            used.update(re.findall(r"'([^']*)'", match.group(0)))
    assert used, "no data_grain filter found — has the canvas fact stopped using it?"
    assert used <= EXPECTED_DATA_GRAINS, (
        f"registry filters on data_grain value(s) the health check does not expect: "
        f"{sorted(used - EXPECTED_DATA_GRAINS)}"
    )


# --------------------------------------------------------------------------------------
# The MCP tool roster
# --------------------------------------------------------------------------------------


def test_expected_tool_count_matches_the_registered_tools() -> None:
    """`mcp_self_check` hard-fails for every user when this drifts, so it must be
    bumped in the same commit as a tool addition or removal."""
    from bpd_mcp.server import mcp

    n = len(mcp._tool_manager._tools)
    assert n == EXPECTED_TOOL_COUNT, (
        f"tool count drift: server registers {n} tools, tools/admin.py expects "
        f"{EXPECTED_TOOL_COUNT}. Tools: {sorted(mcp._tool_manager._tools)}"
    )


def test_tool_roster_is_the_post_bigquery_fourteen() -> None:
    """Lineage: 22 tools before the swap, minus the four Kiteworks discovery
    tools, minus sync/refresh/reingest, minus clear_cache = 14."""
    from bpd_mcp.server import mcp

    assert EXPECTED_TOOL_COUNT == 14
    assert set(mcp._tool_manager._tools) == {
        "bpd_list_datasets",
        "bpd_run_sql",
        "bpd_export_query_to_csv",
        "bpd_describe_schema",
        "bpd_get_sales_summary",
        "bpd_get_top_skus",
        "bpd_get_inventory_snapshot",
        "bpd_get_sell_through",
        "bpd_get_open_orders",
        "bpd_get_upcoming_pos",
        "bpd_get_forecast_vs_actual",
        "bpd_bigquery_status",
        "bpd_data_freshness",
        "bpd_health_check",
    }


# --------------------------------------------------------------------------------------
# Declared projection vs actual projection
# --------------------------------------------------------------------------------------
#
# `date_column` and `column_contract` are DECLARATIONS about what a body
# projects, and nothing enforced them. A body that stops projecting its declared
# date column does not fail loudly: `detect_date_column` keeps returning the old
# name, the date-range sweep silently drops that table, and every listing shows
# a null range.
#
# The parser below reads the FIRST top-level SELECT of a body — which is what
# names the output columns, including for a `UNION ALL`, where later branches
# contribute values but not names. It is deliberately strict: an input shape it
# was not written for (a leading `WITH`, a `SELECT *`) raises rather than
# returning a partial list that would make the guard vacuous. The `-m bq` test
# at the end pins its output to BigQuery's own answer for every registry entry.


_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def _skip_quoted(sql: str, i: int) -> int:
    """Index just past the string/backtick literal starting at `i`."""
    n = len(sql)
    quote = sql[i]
    j = i + 1
    while j < n and sql[j] != quote:
        j += 2 if sql[j] == "\\" and quote != "`" else 1
    return min(j + 1, n)


def _strip_comments(sql: str) -> str:
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "`'\"":
            j = _skip_quoted(sql, i)
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def projected_columns(sql: str) -> list[str]:
    """Output column names of the first top-level SELECT in `sql`."""
    s = _strip_comments(sql).strip()
    if re.match(r"(?is)^with\b", s):
        raise ValueError("body starts with WITH — this parser reads a bare SELECT only")
    head = re.match(r"(?is)^select\s+(?:distinct\s+)?", s)
    if head is None:
        raise ValueError(f"body does not start with SELECT: {s[:60]!r}")

    items: list[str] = []
    cur: list[str] = []
    depth = 0
    i, n = head.end(), len(s)
    while i < n:
        ch = s[i]
        if ch in "`'\"":
            j = _skip_quoted(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                items.append("".join(cur))
                cur = []
                i += 1
                continue
            word = _WORD.match(s, i)
            if word is not None and word.group(0).upper() == "FROM":
                items.append("".join(cur))
                break
        cur.append(ch)
        i += 1
    else:
        raise ValueError("no top-level FROM found in body")

    return [_output_name(item) for item in items]


def _output_name(item: str) -> str:
    """The name a select-list item projects: its alias, else the bare column."""
    tokens: list[str] = []
    i, n = 0, len(item)
    while i < n:
        ch = item[i]
        if ch.isspace():
            i += 1
        elif ch == "`":
            j = _skip_quoted(item, i)
            tokens.append(item[i:j])
            i = j
        else:
            word = _WORD.match(item, i)
            if word is not None:
                tokens.append(word.group(0))
                i = word.end()
            else:
                tokens.append(ch)
                i += 1
    if not tokens:
        raise ValueError(f"empty select-list item in {item!r}")
    if tokens[-1] == "*":
        raise ValueError("SELECT * projects an unknown column set — this guard cannot read it")
    return tokens[-1].strip("`")


def test_the_projection_parser_refuses_shapes_it_cannot_read() -> None:
    """Self-test. A guard that returned [] for an unfamiliar body would pass
    every assertion below while checking nothing."""
    assert projected_columns("SELECT a, b AS c FROM `p.d.t`") == ["a", "c"]
    assert projected_columns("SELECT `Last Remodel Date` AS last_remodel_date FROM `p.d.t`") == [
        "last_remodel_date"
    ]
    # A comma inside a function call is not a select-list separator.
    assert projected_columns("SELECT COALESCE(a, b) AS x, c FROM `p.d.t`") == ["x", "c"]
    # 'FROM' inside a string literal is not the FROM clause.
    assert projected_columns("SELECT 'FROM' AS lit, a FROM `p.d.t`") == ["lit", "a"]
    for unreadable in ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT * FROM `p.d.t`"):
        with pytest.raises(ValueError):
            projected_columns(unreadable)


def test_every_declared_date_column_is_actually_projected() -> None:
    """`detect_date_column` returns this name without probing, so a declaration
    the body does not project makes every date filter reference a column that
    does not exist."""
    missing = {
        name: entry.date_column
        for name, entry in LOGICAL_TABLES.items()
        if entry.date_column not in projected_columns(entry.sql)
    }
    assert missing == {}, f"date_column declared but not projected: {missing}"


def test_every_column_contract_entry_is_actually_projected() -> None:
    """`column_contract` is what callers may rely on. Documentary is not the same
    as unchecked."""
    missing = {
        name: sorted(set(entry.column_contract) - set(projected_columns(entry.sql)))
        for name, entry in LOGICAL_TABLES.items()
        if not set(entry.column_contract) <= set(projected_columns(entry.sql))
    }
    assert missing == {}, f"column_contract entries not projected: {missing}"


def test_every_registry_entry_declares_a_column_contract() -> None:
    """An empty contract silently opts a table out of the guard above."""
    empty = sorted(name for name, e in LOGICAL_TABLES.items() if not e.column_contract)
    assert empty == [], f"logical table(s) with no column_contract: {empty}"


def test_registry_bodies_project_lowercase_names() -> None:
    """The LogicalTable docstring requires it, and the role registry's candidate
    lists are matched case-insensitively only because of it. Several bpd_raw
    sources ship SHOUTING column names, so every one of them needs an alias."""
    shouting = {
        name: [c for c in projected_columns(entry.sql) if c != c.lower()]
        for name, entry in LOGICAL_TABLES.items()
        if any(c != c.lower() for c in projected_columns(entry.sql))
    }
    assert shouting == {}, f"logical tables projecting non-lowercase columns: {shouting}"


# --------------------------------------------------------------------------------------
# Same declarations, checked against BigQuery itself (0 bytes: dry runs only)
# --------------------------------------------------------------------------------------


@pytest.mark.bq
def test_parsed_projection_matches_bigquerys_own_answer(bq_client) -> None:
    """Pins the pure-python parser to the engine.

    Without this the free guards above could agree with a misparse forever. One
    cached `SELECT * FROM (<body>) LIMIT 0` dry run per table, 0 bytes billed.
    """
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    mismatches: dict[str, tuple[list[str], list[str]]] = {}
    for name, entry in LOGICAL_TABLES.items():
        parsed = projected_columns(entry.sql)
        actual = [c for c, _ in wh.logical_schema(name)]
        if parsed != actual:
            mismatches[name] = (parsed, actual)
    assert mismatches == {}, f"parser disagrees with BigQuery: {mismatches}"


@pytest.mark.bq
def test_declared_date_columns_exist_in_the_live_schema(bq_client) -> None:
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    for name, entry in LOGICAL_TABLES.items():
        columns = {c for c, _ in wh.logical_schema(name)}
        assert entry.date_column in columns, (
            f"{name}.date_column={entry.date_column!r} is not in the live projection "
            f"{sorted(columns)}"
        )
        assert set(entry.column_contract) <= columns, (
            f"{name}.column_contract missing from live projection: "
            f"{sorted(set(entry.column_contract) - columns)}"
        )


# `heuristic_date_column`'s docstring documents this divergence and asks a drift
# test to allow-list rather than "fix" it: location_attr declares
# `last_remodel_date` (the DuckDB-parity answer, and what COLUMN_ROLES' date role
# points at), while the schema-only heuristic lands on `store_open_date` because
# BigQuery keeps the raw column as STRING and tier 1 skips it. Following the
# heuristic would silently move every location_attr date range.
_HEURISTIC_DIVERGENCE = {"location_attr": ("last_remodel_date", "store_open_date")}


@pytest.mark.bq
def test_declared_date_columns_agree_with_the_schema_heuristic(bq_client) -> None:
    """The heuristic is off the query path now; it survives exactly so this test
    can assert each declaration still matches what the live schema implies. A new
    divergence means either the declaration or the upstream types moved."""
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    unexpected: dict[str, tuple[str, str | None]] = {}
    for name, entry in LOGICAL_TABLES.items():
        columns = wh.logical_schema(name)
        implied = heuristic_date_column(name, columns)
        if implied == entry.date_column:
            continue
        if _HEURISTIC_DIVERGENCE.get(name) == (entry.date_column, implied):
            continue
        unexpected[name] = (entry.date_column, implied)
    assert unexpected == {}, (
        "declared date_column no longer agrees with the schema heuristic "
        f"(declared, implied): {unexpected}"
    )


@pytest.mark.bq
def test_the_documented_heuristic_divergence_still_exists(bq_client) -> None:
    """The allow-list above must not outlive the divergence it excuses.

    If BigQuery ever types `Last Remodel Date` as a DATE, the heuristic starts
    agreeing and the entry becomes a hole in the previous test.
    """
    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    for name, (declared, implied) in _HEURISTIC_DIVERGENCE.items():
        assert LOGICAL_TABLES[name].date_column == declared
        assert heuristic_date_column(name, wh.logical_schema(name)) == implied, (
            f"{name} no longer diverges from the heuristic — drop it from "
            "_HEURISTIC_DIVERGENCE so the guard covers it again"
        )


# --------------------------------------------------------------------------------------
# The test suite's own parallel source of truth: conftest._TYPES
# --------------------------------------------------------------------------------------

# `_TYPES` is the fourth declaration that has to agree with the warehouse, and
# the only one that lives in the test suite. Its docstring states the contract —
# "pin the production types explicitly so a fixture cannot accidentally test a
# type production never produces" — but a map maintained by hand drifts silently
# in the one direction that matters: a column MISSING from it falls through to
# `STRING if isinstance(value, str)`, and a STRING date routes
# `ResolvedColumn.select_as_date()` down its SAFE_CAST branch. The fixture then
# passes while proving nothing about the plain-DATE branch production actually
# takes. Nothing warns; the test just quietly stops testing what it names.

# Two entries are RAW source columns that no registry body projects, because the
# body aliases them (`inventory_date AS business_d`,
# `fiscal_week_end_date AS fiscal_week_end_d`). Both are live COLUMN_ROLES
# candidates, so a fixture may legitimately stand in for either side; they are
# checked against the SOURCE table's schema instead of the projection.
_RAW_SOURCE_TYPE_COLUMNS = {
    "inventory_date": "biom-reporting-s26.biom_canvas.fct_target_inventory",
    "fiscal_week_end_date": "biom-reporting-s26.biom_canvas.fct_target_gross_margin",
}

# `logical_schema` reports the client's legacy field-type spelling; conftest
# writes standard-SQL casts. Same types, two names.
_LEGACY_TYPE_SPELLING = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
}


def test_raw_source_type_pins_name_columns_no_registry_body_projects() -> None:
    """The allow-list must not outlive its reason.

    If a body stops aliasing one of these away, the entry becomes an untested
    hole in the live guard below rather than an excused one.
    """
    from tests.conftest import _TYPES

    for column in _RAW_SOURCE_TYPE_COLUMNS:
        assert column in _TYPES, f"{column} is allow-listed but not in _TYPES"
        projecting = [
            n for n, e in LOGICAL_TABLES.items() if column in projected_columns(e.sql)
        ]
        assert projecting == [], (
            f"{column} is now projected by {projecting} — drop it from "
            "_RAW_SOURCE_TYPE_COLUMNS so the live guard covers it directly"
        )

    # Dead weight is the other failure mode: an entry nothing can reach.
    candidates = {c for m in cr.COLUMN_ROLES.values() for cands in m.values() for c in cands}
    assert set(_RAW_SOURCE_TYPE_COLUMNS) <= candidates, (
        "a raw-source pin that no role can resolve to is unreachable from any "
        f"fixture: {sorted(set(_RAW_SOURCE_TYPE_COLUMNS) - candidates)}"
    )


@pytest.mark.bq
def test_conftest_type_pins_match_the_types_production_actually_ships(bq_client) -> None:
    """Every `conftest._TYPES` entry must be the type BigQuery reports.

    Free: the projected columns come from the same cached `LIMIT 0` dry runs the
    guards above use, and the two raw source tables are read with `get_table`,
    a metadata call that scans nothing.

    Changing any pin to a type production does not ship — say
    `sale_quantity` back to INT64, or `last_update_d` to STRING — fails here.
    """
    from tests.conftest import _TYPES

    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    live: dict[str, set[str]] = {}
    for name in LOGICAL_TABLES:
        for column, dtype in wh.logical_schema(name):
            live.setdefault(column, set()).add(
                _LEGACY_TYPE_SPELLING.get(dtype.upper(), dtype.upper())
            )
    for column, table in _RAW_SOURCE_TYPE_COLUMNS.items():
        schema = {f.name: f.field_type for f in bq_client.get_table(table).schema}
        assert column in schema, f"{column} is gone from {table}"
        live.setdefault(column, set()).add(
            _LEGACY_TYPE_SPELLING.get(schema[column].upper(), schema[column].upper())
        )

    unknown = sorted(set(_TYPES) - set(live))
    assert unknown == [], (
        "_TYPES pins columns that exist nowhere in the warehouse — a fixture "
        f"using one is testing a shape production cannot produce: {unknown}"
    )
    wrong = {c: (pinned, sorted(live[c])) for c, pinned in _TYPES.items() if live[c] != {pinned}}
    assert wrong == {}, f"_TYPES disagrees with production (pinned, live): {wrong}"


@pytest.mark.bq
def test_every_resolvable_date_column_is_pinned(bq_client) -> None:
    """The gap direction: a production DATE column that `_TYPES` forgets.

    A missing pin is worse than a wrong one, because it fails open: the value
    falls through to STRING, `select_as_date()` takes its SAFE_CAST branch, and
    the fixture goes green having never exercised the plain-DATE branch that
    production actually runs. Both `processed_ct_date` and
    `original_estimated_arrival_d` were sitting in exactly that hole when this
    guard was written.

    The rule needs no list of "date-ish role names" to keep current: a column
    only reaches `select_as_date()` by being a COLUMN_ROLES candidate, so every
    candidate BigQuery types as DATE must be pinned, whatever role names it up.
    """
    from tests.conftest import _TYPES

    wh = BigQueryWarehouse(client=bq_client, registry=dict(LOGICAL_TABLES))
    candidates = {c for m in cr.COLUMN_ROLES.values() for cands in m.values() for c in cands}
    missing = sorted(
        {
            column
            for name in LOGICAL_TABLES
            for column, dtype in wh.logical_schema(name)
            if dtype.upper() == "DATE" and column in candidates and column not in _TYPES
        }
    )
    assert missing == [], (
        "production DATE columns a role can resolve to, unpinned in "
        f"conftest._TYPES — fixtures using them silently become STRING: {missing}"
    )
