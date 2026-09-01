"""Date-column detection after the BigQuery swap.

There are now two functions where DuckDB had one, and the difference is the
whole point of the file:

  `BigQueryWarehouse.detect_date_column(table)`
      Returns the registry's DECLARED `LogicalTable.date_column`. It reads no
      schema and runs no query. The DuckDB version probed — one
      `SELECT COUNT(col)` per candidate column per call, to implement the
      "an all-NULL column must not win" guard. Free on a local file; a full
      column scan each on BigQuery, fanned out over 15 tables every time
      `list_datasets` ran. Declaring the answer removes the cost AND the hazard
      the probe existed for.

  `bq.heuristic_date_column(table, columns)`
      The old tiering rules, schema-only. Off the query path entirely; it
      survives so a drift test can check each declaration still agrees with what
      the live schema implies, and so a newly added logical table has a
      principled default to declare.

The tiers, earliest ordinal wins within a tier:
    1. a DATE/TIMESTAMP-typed column
    2. a name ending `_date` / `_dt` / `_d`
    3. a name containing date | week | period | as_of | effective
    4. `COLUMN_ROLES[table]["date"]`, consulted LAST so the generic heuristic
       wins for a table nobody has declared roles for
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from bpd_mcp.bq import (
    LOGICAL_TABLES,
    BigQueryWarehouse,
    LogicalTable,
    heuristic_date_column,
)
from bpd_mcp.column_roles import COLUMN_ROLES

# A stand-in for a real client. Nothing in this file's pure-python tier issues a
# query, so the warehouse never touches it.
_NO_CLIENT: Any = object()


def _registry_warehouse(**tables: LogicalTable) -> BigQueryWarehouse:
    return BigQueryWarehouse(client=_NO_CLIENT, registry=dict(tables))


def _table(name: str, date_column: str) -> LogicalTable:
    return LogicalTable(
        name=name,
        sql=f"SELECT 1 -- {name}",
        base_tables=(f"proj.ds.{name}",),
        date_column=date_column,
    )


# ---------------------------------------------------------------------------
# heuristic_date_column — the tiering rules (pure python)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column_name", "sql_type"),
    [
        # Tier 1 — typed.
        ("last_update_d", "DATE"),
        ("snapshot_dt", "TIMESTAMP"),
        ("order_date", "DATE"),
        ("business_d", "DATETIME"),
        # Tier 2 — Target's `_d` / `_date` suffix convention, stored as STRING.
        ("fiscal_week_begin_d", "STRING"),
        ("processed_ct_d", "STRING"),
        ("week_end_date", "STRING"),
        ("period_end_date", "STRING"),
        ("as_of_date", "STRING"),
        # Tier 3 — a date token somewhere in the name, no suffix.
        ("report_date_dim", "STRING"),
        ("effective_date_dim", "STRING"),
        ("fiscal_week_indicator", "STRING"),
    ],
)
def test_heuristic_picks_up_suffix_and_token_styles(column_name: str, sql_type: str) -> None:
    columns = [("tcin", "INTEGER"), ("irrelevant_col", "STRING"), (column_name, sql_type)]
    assert heuristic_date_column("probe", columns) == column_name


def test_heuristic_prefers_a_typed_column_over_a_suffix_match() -> None:
    """Tier 1 beats tier 2 even when the typed column's NAME looks nothing like a date."""
    columns = [("week_end_d", "STRING"), ("created", "TIMESTAMP")]
    assert heuristic_date_column("probe", columns) == "created"


def test_heuristic_prefers_a_suffix_over_a_substring() -> None:
    """Tier 2 beats tier 3, regardless of column order."""
    columns = [("week_label", "STRING"), ("fiscal_week_begin_d", "STRING")]
    assert heuristic_date_column("probe", columns) == "fiscal_week_begin_d"


def test_heuristic_breaks_ties_on_ordinal_within_a_tier() -> None:
    columns = [
        ("tcin", "INTEGER"),
        ("beginning_d", "STRING"),
        ("ending_d", "STRING"),
    ]
    assert heuristic_date_column("probe", columns) == "beginning_d"
    # ...and a later DATE-typed column still outranks both (different tier).
    assert (
        heuristic_date_column("probe", [*columns, ("stamped", "DATE")]) == "stamped"
    )


def test_heuristic_falls_back_to_column_roles_last(monkeypatch: Any) -> None:
    """Tier 4 exists for a column whose NAME carries no date signal at all."""
    monkeypatch.setitem(COLUMN_ROLES, "probe_ds", {"date": ["biz_stamp"]})
    columns = [("tcin", "INTEGER"), ("biz_stamp", "STRING")]
    assert heuristic_date_column("probe_ds", columns) == "biz_stamp"
    # Tier 4 is keyed on the TABLE name, so the same columns under a different
    # table resolve to nothing.
    assert heuristic_date_column("other_ds", columns) is None


def test_heuristic_tier_four_matches_case_insensitively(monkeypatch: Any) -> None:
    monkeypatch.setitem(COLUMN_ROLES, "probe_ds", {"date": ["biz_stamp"]})
    assert heuristic_date_column("probe_ds", [("BIZ_STAMP", "STRING")]) == "BIZ_STAMP"


def test_heuristic_returns_none_for_a_dateless_table() -> None:
    columns = [("tcin", "INTEGER"), ("name", "STRING"), ("price", "NUMERIC")]
    assert heuristic_date_column("probe", columns) is None


def test_heuristic_returns_none_for_no_columns() -> None:
    assert heuristic_date_column("probe", []) is None


def test_every_declared_date_candidate_is_reachable_without_tier_four() -> None:
    """Documents why tier 4 never fires in production today.

    Every name in every `COLUMN_ROLES[...]["date"]` list already satisfies
    tier 2 or tier 3, so tier 4 is a safety net for a FUTURE logical table with
    an opaque date column (see the extension seam in bq.py), not live
    behaviour. If someone adds an opaque candidate, this test tells them tier 4
    is now load-bearing.
    """
    tokens = ("date", "week", "period", "as_of", "effective")
    for ds, roles in COLUMN_ROLES.items():
        for cand in roles.get("date", []):
            low = cand.lower()
            assert low.endswith(("_date", "_dt", "_d")) or any(t in low for t in tokens), (
                f"{ds}.date candidate {cand!r} is only reachable via tier 4"
            )


# ---------------------------------------------------------------------------
# detect_date_column — the registry's DECLARED answer
# ---------------------------------------------------------------------------


def test_detect_date_column_returns_the_declaration() -> None:
    wh = _registry_warehouse(sales_daily=_table("sales_daily", "sales_date"))
    assert wh.detect_date_column("sales_daily") == "sales_date"


def test_detect_date_column_does_not_read_the_schema_or_the_rows() -> None:
    """It is a dict lookup: no dry run, no query, no client call, 0 bytes.

    The client is a bare `object()` here, so any attempt to run something would
    raise AttributeError rather than quietly costing money.
    """
    wh = _registry_warehouse(sales_daily=_table("sales_daily", "sales_date"))
    assert wh.detect_date_column("sales_daily") == "sales_date"
    # Proof that it took the dict-lookup path and not the schema path: the
    # schema path on this same warehouse blows up, because there is no client.
    with pytest.raises(AttributeError):
        wh.logical_schema("sales_daily")


def test_detect_date_column_ignores_the_heuristic_when_they_disagree() -> None:
    """The declaration wins — which is also how the all-NULL hazard is handled.

    `item_attr_extended.launch_date` is a DATE-typed column that is entirely
    NULL. The heuristic, which sees types and names but no values, picks it and
    every date range downstream reports null. The DuckDB fix was a
    `SELECT COUNT(col)` probe per candidate; declaring the populated column
    instead costs nothing and cannot regress.
    """
    columns = [("launch_date", "DATE"), ("processed_ct_date", "DATE")]
    assert heuristic_date_column("item_attr_extended", columns) == "launch_date"

    wh = _registry_warehouse(
        item_attr_extended=_table("item_attr_extended", "processed_ct_date")
    )
    assert wh.detect_date_column("item_attr_extended") == "processed_ct_date"


def test_detect_date_column_returns_none_for_an_unknown_table() -> None:
    wh = _registry_warehouse(sales_daily=_table("sales_daily", "sales_date"))
    assert wh.detect_date_column("no_such_table") is None


def test_every_logical_table_declares_a_date_column() -> None:
    """A missing declaration would silently null out that dataset's date range."""
    for name, entry in LOGICAL_TABLES.items():
        assert entry.date_column, f"{name} declares no date_column"


def test_declared_date_columns_appear_in_the_column_contract() -> None:
    """Cheap static half of the drift check — no credential needed.

    The live half (`test_declarations_agree_with_the_live_schema`) needs
    BigQuery; this one catches a typo'd declaration in a pull request.
    """
    for name, entry in LOGICAL_TABLES.items():
        if not entry.column_contract:
            continue
        assert entry.date_column in entry.column_contract, (
            f"{name} declares date_column={entry.date_column!r} which is not in "
            f"its column_contract {entry.column_contract}"
        )


# ---------------------------------------------------------------------------
# Live BigQuery: declarations vs. the real projected schema (0 bytes)
# ---------------------------------------------------------------------------

# The one legitimate divergence, allow-listed rather than "fixed".
#
# location_attr declares `last_remodel_date`; the heuristic returns
# `store_open_date`. The declaration is the DuckDB-parity answer: the CSV
# loader coerced `Last Remodel Date` to a DATE column (mapping Target's `""`
# placeholder to NULL), so it won tier 1 on ordinal position, and
# COLUMN_ROLES' `date` role points at it too. BigQuery keeps the raw column as
# STRING, so tier 1 skips it and lands on the next real DATE. Following the
# heuristic here would silently move every location_attr date range.
_ALLOWED_DIVERGENCE = {"location_attr": ("last_remodel_date", "store_open_date")}


@pytest.mark.bq
def test_declarations_agree_with_the_live_schema(bq_client: Any) -> None:
    """Every `date_column` declaration still matches what the heuristic implies.

    `logical_schema` is a dry run of `SELECT * FROM (<body>) LIMIT 0` per table,
    so all 15 tables cost 0 bytes. This is the drift guard the declaration
    mechanism is only safe with: a declaration is a hand-written constant, and
    hand-written constants rot when a source column is renamed.
    """
    wh = BigQueryWarehouse(client=bq_client)
    divergences: dict[str, tuple[str, str | None]] = {}
    for name, entry in LOGICAL_TABLES.items():
        implied = heuristic_date_column(name, wh.logical_schema(name))
        if implied != entry.date_column:
            divergences[name] = (entry.date_column, implied)
    assert divergences == _ALLOWED_DIVERGENCE, (
        "declared date_column no longer matches the live schema. Either the "
        "source was renamed (fix the declaration) or this is a deliberate "
        f"parity choice (add it to _ALLOWED_DIVERGENCE with a reason): {divergences}"
    )


@pytest.mark.bq
def test_declared_date_column_is_actually_projected(bq_client: Any) -> None:
    """A declaration naming a column the body does not project yields nulls, silently."""
    wh = BigQueryWarehouse(client=bq_client)
    for name, entry in LOGICAL_TABLES.items():
        projected = {c for c, _ in wh.logical_schema(name)}
        assert entry.date_column in projected, (
            f"{name} declares date_column={entry.date_column!r}, not in {sorted(projected)}"
        )


@pytest.mark.bq
def test_detected_date_column_drives_the_date_range_sweep(fixture_warehouse: Any) -> None:
    """End to end over literal rows: declaration -> `date_column` -> min/max.

    Also pins the Patch #12 split. `forecast_weekly` is forward-looking: its
    freshness stamp (`last_update_d`, from DATE_RANGE_ROLES' `snapshot` role)
    and its content horizon (`fiscal_week_begin_d`, the `content` role) differ
    by months, and reporting only the first hides how far the forecast reaches.
    A table with no DATE_RANGE_ROLES entry reports content == snapshot.
    """
    from tests.conftest import fixture_table

    forecast = fixture_table(
        "forecast_weekly",
        [
            {"last_update_d": "2026-07-20", "fiscal_week_begin_d": "2026-07-19",
             "tcin": 100, "location_id": 1, "selected_forecast_q": 10},
            {"last_update_d": "2026-07-20", "fiscal_week_begin_d": "2026-10-11",
             "tcin": 100, "location_id": 1, "selected_forecast_q": 20},
            {"last_update_d": "2026-06-01", "fiscal_week_begin_d": "2026-06-28",
             "tcin": 100, "location_id": 1, "selected_forecast_q": 5},
        ],
        date_column="last_update_d",
    )
    sales = fixture_table(
        "sales_weekly",
        [
            {"sales_date": "2026-07-20", "tcin": 100, "location_id": 1,
             "sale_quantity": 9, "sale_amount": 90},
            {"sales_date": "2026-07-27", "tcin": 100, "location_id": 1,
             "sale_quantity": 4, "sale_amount": 40},
        ],
        date_column="sales_date",
    )
    wh = fixture_warehouse(forecast_weekly=forecast, sales_weekly=sales)

    ranges = wh._date_ranges()

    fc = ranges["forecast_weekly"]
    assert fc["date_column"] == "last_update_d"
    assert fc["min_date"] == date(2026, 6, 1)
    assert fc["max_date"] == date(2026, 7, 20)  # freshness, not horizon
    assert fc["content_column"] == "fiscal_week_begin_d"
    assert fc["content_max_date"] == date(2026, 10, 11)  # horizon, months later

    # No DATE_RANGE_ROLES entry -> content collapses onto the snapshot column.
    sw = ranges["sales_weekly"]
    assert sw["date_column"] == "sales_date"
    assert sw["content_column"] == "sales_date"
    assert sw["max_date"] == sw["content_max_date"] == date(2026, 7, 27)
