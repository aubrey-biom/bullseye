"""SQL-safety tests — the validator (layers 1-3) and the credential layer (layer 4).

What changed with the BigQuery swap, and what these tests now pin:

  * `_ALLOWED_LEAD` is `("SELECT", "WITH")` and nothing else. The DuckDB-era
    validator also allowed EXPLAIN / DESCRIBE / SHOW and a PRAGMA allow-list.
    All four are now REJECTED, and the tests that used to assert they were
    accepted have been inverted rather than deleted — accepting them would be a
    regression, not a nicety: BigQuery has no PRAGMA and no SHOW, rejects
    EXPLAIN outright ("Statement not supported: ExplainStatement"), and the
    supported route to catalogue data is `INFORMATION_SCHEMA`, which is a plain
    SELECT and therefore still allowed.
  * ASSERT / DECLARE / SET / EXECUTE / SCRIPT joined `_FORBIDDEN_TOKENS`. These
    are GoogleSQL *scripting* verbs with no DuckDB equivalent, so the ported
    token list did not cover them; `EXECUTE IMMEDIATE 'DROP TABLE …'` is a
    single statement that builds its DDL as a string, which is precisely the
    shape a keyword scan exists to stop.
  * Layer 4 is no longer "the connection was opened read_only=True". The
    service account holds dataViewer + jobUser, so a write is refused by IAM
    before a job is ever created. `test_credential_layer_refuses_ddl` proves
    that against real BigQuery for 0 bytes and 0 side effects — a *dry run* of
    `CREATE TABLE` is enough to draw the 403, so nothing is created even in the
    failure case where the credential turns out to be writable.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core import exceptions as gexc

from bpd_mcp import sql_safety
from bpd_mcp.schemas import RunSqlInput
from bpd_mcp.sql_safety import SqlBlocked, validate, wrap_with_limit
from bpd_mcp.tools.query import run_sql

# ---------------------------------------------------------------------------
# Layer 0: the allow-list itself
# ---------------------------------------------------------------------------


def test_allowed_lead_is_exactly_select_and_with() -> None:
    """A pin, because widening this tuple is how every other layer gets bypassed."""
    assert sql_safety._ALLOWED_LEAD == ("SELECT", "WITH")


def test_pragma_allowlist_is_gone() -> None:
    """The DuckDB PRAGMA allow-list and its whole branch must stay deleted.

    BigQuery has no PRAGMA statement, so an allow-list of "safe" PRAGMAs is
    dead code that can only ever re-open a hole.
    """
    pragma_attrs = [n for n in dir(sql_safety) if "PRAGMA" in n.upper()]
    assert pragma_attrs == [], f"PRAGMA machinery regrew: {pragma_attrs}"


@pytest.mark.parametrize(
    "token", ["ASSERT", "DECLARE", "SET", "EXECUTE", "SCRIPT"]
)
def test_googlesql_scripting_verbs_are_forbidden(token: str) -> None:
    assert token in sql_safety._FORBIDDEN_TOKENS


# ---------------------------------------------------------------------------
# Layers 1-3: the validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Classic DML/DDL — still blocked, still by leading keyword.
        "INSERT INTO sales_daily VALUES (1)",
        "UPDATE sales_daily SET sale_quantity = 0",
        "DELETE FROM sales_daily WHERE TRUE",
        "TRUNCATE TABLE sales_daily",
        "CREATE TABLE x AS SELECT 1",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "DROP TABLE sales_daily",
        "ALTER TABLE sales_daily ADD COLUMN x INT64",
        # GoogleSQL-specific writes that have no DuckDB equivalent.
        "MERGE INTO sales_daily t USING s ON t.tcin = s.tcin WHEN MATCHED THEN DELETE",
        "EXPORT DATA OPTIONS(uri='gs://bucket/x') AS SELECT 1",
        "GRANT `roles/bigquery.dataViewer` ON TABLE sales_daily TO 'user:x@y.com'",
        "REVOKE `roles/bigquery.dataViewer` ON TABLE sales_daily FROM 'user:x@y.com'",
        "CALL `proj.dataset.some_procedure`()",
        # GoogleSQL scripting — single statements that assemble their own DDL.
        "EXECUTE IMMEDIATE 'DROP TABLE sales_daily'",
        "DECLARE victim STRING DEFAULT 'sales_daily'",
        "SET victim = 'sales_daily'",
        "ASSERT (SELECT COUNT(*) FROM sales_daily) > 0",
        # DuckDB-era file/extension escapes. Meaningless on BigQuery but the
        # tokens cost nothing to keep banned, and the SQL is still not a read.
        "ATTACH 'evil.db' AS evil",
        "COPY sales_daily TO '/tmp/leak.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "VACUUM",
    ],
)
def test_validator_rejects_writes(sql: str) -> None:
    with pytest.raises(SqlBlocked):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT 1",
        "DESCRIBE sales_daily",
        "SHOW TABLES",
        "PRAGMA table_info('sales_daily')",
        "PRAGMA enable_external_access = 1",
    ],
)
def test_validator_rejects_introspection_statements(sql: str) -> None:
    """Inverted from the DuckDB suite, which asserted these were ALLOWED.

    None of them is a BigQuery statement: EXPLAIN is refused by the engine
    ("Statement not supported: ExplainStatement"), and SHOW / DESCRIBE / PRAGMA
    do not exist at all. Letting them through the validator would trade a clear
    SQL_BLOCKED for an opaque engine error, and would keep a non-SELECT lead
    keyword alive in the allow-list for no benefit.
    """
    with pytest.raises(SqlBlocked) as ei:
        validate(sql)
    # Assert WHICH layer fired: the lead-keyword check, not a token match.
    assert "leading keyword" in str(ei.value)


def test_information_schema_is_the_supported_introspection_route() -> None:
    """The replacement for SHOW/DESCRIBE is a plain SELECT, so it still passes."""
    sql = (
        "SELECT column_name, data_type FROM "
        "`biom-reporting-s26.bpd_raw.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE table_name = 'weekly_item_mta'"
    )
    assert validate(sql) == sql


@pytest.mark.parametrize(
    ("sql", "token"),
    [
        # Each of these leads with SELECT, so ONLY the token scan can catch it.
        # Drop the token from _FORBIDDEN_TOKENS and the case goes green.
        ("SELECT 1 /* then */ EXECUTE IMMEDIATE 'DROP TABLE sales_daily'", "EXECUTE"),
        ("SELECT 1 -- x\n DECLARE v STRING", "DECLARE"),
        ("SELECT 1 /* x */ SET v = 2", "SET"),
        ("SELECT 1 /* x */ ASSERT FALSE", "ASSERT"),
        ("SELECT 1 /* x */ SCRIPT", "SCRIPT"),
        ("SELECT 1 /* x */ MERGE INTO t USING s", "MERGE"),
    ],
)
def test_token_scan_catches_verbs_hidden_behind_a_select_lead(sql: str, token: str) -> None:
    with pytest.raises(SqlBlocked) as ei:
        validate(sql)
    msg = str(ei.value)
    assert "forbidden keyword" in msg, f"wrong layer fired for {token}: {msg}"
    # Exactly this token, so the case cannot pass on some OTHER forbidden word
    # that happens to be in the payload.
    assert msg.endswith(f"['{token}']"), msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DELETE FROM sales_daily",
        "SELECT 1 -- ; DROP TABLE x\n; DELETE FROM sales_daily",
        "BEGIN; SELECT 1; COMMIT",
        "DECLARE d DATE; EXECUTE IMMEDIATE 'DROP TABLE sales_daily'",
    ],
)
def test_validator_rejects_multiple_statements(sql: str) -> None:
    with pytest.raises(SqlBlocked):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "/* sneaky */ DROP TABLE sales_daily",
        "/* multi\nline */ INSERT INTO sales_daily VALUES (1)",
        "-- comment\nDROP TABLE sales_daily",
        "/*a*/ /*b*/ TRUNCATE TABLE sales_daily",
    ],
)
def test_validator_strips_comments_before_scanning(sql: str) -> None:
    """Layer 3: a comment must not be able to hide the leading keyword."""
    with pytest.raises(SqlBlocked):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM sales_daily WHERE tcin = 1",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        # The BigQuery dialect the ported tools actually emit: backtick
        # identifiers, SAFE_CAST, and Monday-start week bucketing.
        "SELECT `sale_quantity` FROM sales_daily",
        "SELECT SAFE_CAST(`last_remodel_date` AS DATE) AS d FROM location_attr",
        "SELECT DATE_TRUNC(sales_date, WEEK(MONDAY)) AS wk, SUM(sale_quantity) AS u "
        "FROM sales_daily GROUP BY wk",
        "SELECT tcin FROM orders_daily QUALIFY ROW_NUMBER() OVER "
        "(PARTITION BY tcin ORDER BY snapshot_d DESC) = 1",
        # Strings that merely LOOK dangerous.
        "SELECT '/* not a comment */' AS s",
        "SELECT 'has ; in string' AS s",
        "SELECT 'DROP TABLE sales_daily' AS s",
    ],
)
def test_validator_accepts_reads(sql: str) -> None:
    assert validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   \n\t ",
        "-- nothing but a comment",
        "/* nothing but a comment */",
        ";;;",
        "'just a string literal'",
    ],
)
def test_validator_rejects_sql_with_no_statement(sql: str) -> None:
    with pytest.raises(SqlBlocked):
        validate(sql)


def test_validate_returns_the_original_text_not_the_scrubbed_copy() -> None:
    """Comment/string stripping is for SCANNING only.

    The returned string is what actually executes, so a validator that returned
    `cleaned` would silently rewrite the user's query — replacing every string
    literal with `''`.
    """
    sql = "SELECT 'keep me' AS s -- and this comment\n"
    assert validate(sql) == "SELECT 'keep me' AS s -- and this comment"
    # Surrounding whitespace and a trailing semicolon are trimmed (in that
    # order, so `SELECT 1 ;` keeps its interior space — harmless, and
    # `wrap_with_limit` nests the result in parentheses anyway).
    out = validate("  SELECT 1 ;  ")
    assert out.strip() == "SELECT 1"
    assert not out.rstrip().endswith(";")


def test_known_false_positives_are_rejected_by_design() -> None:
    """The scan is token-exact and deliberately over-eager.

    `REPLACE` is both a DDL verb (`CREATE OR REPLACE`) and a very ordinary
    string function, and `SET` is a plausible column name. Both are blocked.
    The module docstring makes this trade explicitly ("false positives are
    preferable to false negatives"), so it is pinned rather than papered over:
    if someone decides to relax it, this test is where the decision gets made.
    """
    for sql in [
        "SELECT REPLACE(dpci, '-', '') AS clean FROM item_attr",
        "SELECT * EXCEPT (dpci) REPLACE (tcin + 1 AS tcin) FROM item_attr",
        "SELECT `set` FROM item_attr",
    ]:
        with pytest.raises(SqlBlocked):
            validate(sql)


# ---------------------------------------------------------------------------
# wrap_with_limit
# ---------------------------------------------------------------------------


def test_wrap_with_limit_uses_subquery() -> None:
    out = wrap_with_limit("SELECT * FROM sales_daily LIMIT 5", 100)
    assert out == "SELECT * FROM (SELECT * FROM sales_daily LIMIT 5) AS _bpd_sub LIMIT 100"


def test_wrap_with_limit_strips_trailing_semicolon() -> None:
    """A semicolon left inside the subquery would be a syntax error."""
    out = wrap_with_limit("SELECT 1 ;", 10)
    assert ";" not in out


def test_wrap_with_limit_coerces_limit_to_int() -> None:
    """The limit is interpolated, not parameterised — `int()` is the guard."""
    assert wrap_with_limit("SELECT 1", "50").endswith("LIMIT 50")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        wrap_with_limit("SELECT 1", "10; DROP TABLE x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer 4a: run_sql's own gate (pure python — nothing reaches BigQuery)
# ---------------------------------------------------------------------------


class _TripwireWarehouse:
    """A warehouse that fails the test if any query reaches it."""

    def __init__(self, *, read_only: bool = True) -> None:
        self.read_only = read_only

    def dry_run(self, sql: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError(f"blocked SQL reached BigQuery via dry_run: {sql!r}")

    def execute_sql(self, sql: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError(f"blocked SQL reached BigQuery via execute_sql: {sql!r}")


@pytest.mark.parametrize(
    "bad_sql",
    [
        "DROP TABLE sales_daily",
        "INSERT INTO sales_daily VALUES (99)",
        "SELECT 1; DELETE FROM sales_daily",
        "/*x*/ DROP TABLE sales_daily",
        "EXECUTE IMMEDIATE 'DROP TABLE sales_daily'",
        "EXPLAIN SELECT 1",
        "SHOW TABLES",
    ],
)
async def test_run_sql_tool_blocks_before_touching_bigquery(bad_sql: str) -> None:
    resp = await run_sql(_TripwireWarehouse(), RunSqlInput(sql=bad_sql))
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == "SQL_BLOCKED", f"failed to block: {bad_sql!r}"


async def test_run_sql_tool_refuses_a_non_read_only_warehouse() -> None:
    """Belt-and-suspenders: `read_only` falsy means refuse everything, even SELECT."""
    resp = await run_sql(_TripwireWarehouse(read_only=False), RunSqlInput(sql="SELECT 1"))
    assert resp.ok is False
    assert resp.error is not None
    assert resp.error.code == "SQL_BLOCKED"
    assert "read-only" in resp.error.message


# ---------------------------------------------------------------------------
# Layer 4b: the credential itself (real BigQuery, 0 bytes, 0 side effects)
# ---------------------------------------------------------------------------


@pytest.mark.bq
def test_warehouse_is_always_read_only(bq_client: Any) -> None:
    """`read_only` is a property with no setter — no code path can clear it."""
    from bpd_mcp.bq import BigQueryWarehouse

    wh = BigQueryWarehouse(client=bq_client, registry={})
    assert wh.read_only is True
    with pytest.raises(AttributeError):
        wh.read_only = False  # type: ignore[misc]


@pytest.mark.bq
@pytest.mark.parametrize(
    ("sql", "permission"),
    [
        (
            "CREATE TABLE `biom-reporting-s26.bpd_meta._bpd_safety_probe` (x INT64)",
            "bigquery.tables.create",
        ),
        (
            "INSERT INTO `biom-reporting-s26.bpd_raw.weekly_item_mta` (tcin) VALUES (1)",
            "bigquery.tables.updateData",
        ),
    ],
)
def test_credential_layer_refuses_writes(bq_client: Any, sql: str, permission: str) -> None:
    """The claim in the sql_safety docstring, executed rather than asserted in prose.

    This is a DRY RUN: BigQuery checks IAM while planning, so the 403 arrives
    before any job could run. That keeps the test free (0 bytes) AND safe — if
    the credential were ever wrongly granted write access the test would fail
    loudly without having created or mutated anything.
    """
    from bpd_mcp.bq import BigQueryWarehouse

    wh = BigQueryWarehouse(client=bq_client, registry={})
    with pytest.raises(gexc.Forbidden) as ei:
        wh.dry_run(sql)
    assert permission in str(ei.value)


@pytest.mark.bq
async def test_run_sql_executes_a_select_end_to_end(fixture_warehouse: Any) -> None:
    """The safe path still works: validate -> wrap -> CTE injection -> BigQuery.

    Also covers the ordering hazard `wrap_with_limit` creates — the user's own
    `WITH` ends up nested inside `_bpd_sub` while the injected registry CTEs sit
    outermost, and both must stay in scope.
    """
    wh = fixture_warehouse(
        sales_daily=[
            {"sales_date": "2026-06-01", "tcin": 111, "location_id": 1,
             "sale_quantity": 10, "sale_amount": 100},
            {"sales_date": "2026-06-02", "tcin": 111, "location_id": 1,
             "sale_quantity": 20, "sale_amount": 200},
            {"sales_date": "2026-06-02", "tcin": 222, "location_id": 1,
             "sale_quantity": 5, "sale_amount": 50},
        ]
    )
    resp = await run_sql(
        wh,
        RunSqlInput(
            sql=(
                "WITH per_item AS ("
                "  SELECT tcin, SUM(sale_quantity) AS units FROM sales_daily GROUP BY tcin"
                ") SELECT SUM(units) AS total_units FROM per_item"
            ),
            response_format="json",
        ),
    )
    assert resp.ok is True, resp.error
    assert resp.data["rows"][0]["total_units"] == 35
    # The dry-run gate priced it, and a literal-CTE query scans nothing.
    assert resp.data["estimated_bytes_scanned"] == 0
