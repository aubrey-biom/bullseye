"""Hermetic tests for `bpd_mcp.bq` — CTE injection, quoting, masking, credentials, registry.

Default tier only: pure python, no network, no credentials, no cost. Everything
here is either string manipulation (`build` / `mask_sql` / `quote_ident`) or
filesystem work redirected into `tmp_path`.

These cover the genuinely NEW logic the DuckDB -> BigQuery swap introduced. The
old warehouse had no CTE injection at all (logical tables were physical tables),
and its identifier quoting was a different dialect. That makes this module the
place where a subtle bug is both most likely and least visible: a missed
injection fails loudly at BigQuery, but a SPURIOUS injection silently adds
hundreds of MB of scan, and a wrongly-quoted identifier silently collapses a
GROUP BY to a constant.
"""

from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path

import pytest

from bpd_mcp import bq
from bpd_mcp.bq import (
    LOGICAL_TABLES,
    CircularDependency,
    CredentialsUnavailable,
    LogicalTable,
    build,
    build_with_report,
    caller_defined_ctes,
    heuristic_date_column,
    inject,
    mask_sql,
    quote_ident,
    referenced_logical_tables,
    resolve_credentials,
    resolve_references,
)

# ---------------------------------------------------------------------------
# A tiny synthetic registry. Deliberately NOT the production one: injection
# behaviour must be provable independently of what BPD happens to define today.
# ---------------------------------------------------------------------------


def _t(name: str, sql: str, *, depends_on: tuple[str, ...] = ()) -> LogicalTable:
    return LogicalTable(
        name=name,
        sql=sql,
        base_tables=(),
        date_column="d",
        depends_on=depends_on,
    )


@pytest.fixture
def reg() -> dict[str, LogicalTable]:
    """alpha and gamma are leaves; beta depends on alpha; delta depends on beta."""
    return {
        "alpha": _t("alpha", "SELECT 1 AS x, DATE '2026-01-01' AS d"),
        "beta": _t("beta", "SELECT x, d FROM alpha", depends_on=("alpha",)),
        "gamma": _t("gamma", "SELECT 2 AS x, DATE '2026-01-02' AS d"),
        "delta": _t("delta", "SELECT x FROM beta", depends_on=("beta",)),
    }


def _injected(sql: str, registry: dict[str, LogicalTable]) -> list[str]:
    return build_with_report(sql, registry)[1]


def _with_keyword_count(sql: str) -> int:
    """How many WITH keywords survive in *code* position (comments/strings masked)."""
    return len(re.findall(r"\bWITH\b", mask_sql(sql), re.IGNORECASE))


# ---------------------------------------------------------------------------
# Injection: which tables, and how many
# ---------------------------------------------------------------------------


def test_only_referenced_tables_are_injected(reg):
    """Blanket injection would add ~400 MB of scan to every tool call."""
    sql, order = build_with_report("SELECT * FROM gamma", reg)
    assert order == ["gamma"]
    assert "gamma AS (" in sql
    assert "alpha" not in sql
    assert "beta" not in sql


def test_nothing_referenced_returns_sql_byte_identical(reg):
    original = "SELECT 1"
    assert build(original, reg) == original
    assert build_with_report(original, reg) == (original, [])


def test_empty_bodies_never_emits_an_empty_with(reg):
    assert inject("SELECT 1", {}) == "SELECT 1"


def test_transitive_dependencies_are_pulled_in_topological_order(reg):
    """`WITH` in BigQuery is sequential: a member may only reference EARLIER ones."""
    sql, order = build_with_report("SELECT * FROM delta", reg)
    assert order == ["alpha", "beta", "delta"]
    # gamma is unrelated and must not be dragged along.
    assert "gamma" not in sql
    # And the emitted text really is in that order.
    assert sql.index("alpha AS (") < sql.index("beta AS (") < sql.index("delta AS (")


def test_independent_tables_are_ordered_alphabetically_for_diffability(reg):
    assert _injected("SELECT * FROM gamma g JOIN alpha a USING (x)", reg) == [
        "alpha",
        "gamma",
    ]


def test_body_referencing_a_bare_name_without_declaring_it_still_resolves():
    """`depends_on` is belt; rescanning the body is braces.

    An undeclared edge would otherwise reach BigQuery as `Unrecognized name`.
    """
    undeclared = {
        "alpha": _t("alpha", "SELECT 1 AS x"),
        "beta": _t("beta", "SELECT * FROM alpha"),  # note: no depends_on
    }
    assert _injected("SELECT * FROM beta", undeclared) == ["alpha", "beta"]


def test_circular_dependency_raises(reg):
    cyclic = {
        "ping": _t("ping", "SELECT * FROM pong", depends_on=("pong",)),
        "pong": _t("pong", "SELECT * FROM ping", depends_on=("ping",)),
    }
    with pytest.raises(CircularDependency) as e:
        build("SELECT * FROM ping", cyclic)
    assert "ping" in str(e.value) and "pong" in str(e.value)


def test_self_reference_is_not_a_cycle():
    """A body naming itself is a recursive CTE, not a dependency — `_direct_deps`
    subtracts the table's own name, so it emits once rather than deadlocking."""
    selfref = {"loop": _t("loop", "SELECT * FROM loop")}
    assert _injected("SELECT * FROM loop", selfref) == ["loop"]


# ---------------------------------------------------------------------------
# Injection: splicing into a caller's own WITH
# ---------------------------------------------------------------------------


def test_caller_with_is_spliced_not_nested(reg):
    out = build("WITH z AS (SELECT * FROM alpha) SELECT * FROM z", reg)
    # Exactly one WITH keyword: ours, with the caller's list comma-appended.
    assert _with_keyword_count(out) == 1
    assert out.startswith("WITH alpha AS (")
    assert "),\nz AS (SELECT * FROM alpha) SELECT * FROM z" in out


def test_caller_with_recursive_is_preserved(reg):
    recursive = (
        "WITH RECURSIVE r AS ("
        "SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3"
        ") SELECT * FROM r, alpha"
    )
    out = build(recursive, reg)
    assert out.startswith("WITH RECURSIVE alpha AS (")
    assert _with_keyword_count(out) == 1
    assert "r AS (SELECT 1 AS n" in out


def test_leading_comment_before_caller_with_is_kept(reg):
    out = build("-- note\nWITH z AS (SELECT 1) SELECT * FROM alpha", reg)
    assert out.startswith("-- note\nWITH alpha AS (")
    assert _with_keyword_count(out) == 1


def test_caller_defined_cte_shadows_the_registry(reg):
    """Two CTEs of one name is a hard BigQuery error, and the caller meant theirs."""
    sql = "WITH alpha AS (SELECT 99 AS x) SELECT * FROM alpha"
    assert resolve_references(sql, reg) == frozenset()
    assert build(sql, reg) == sql


def test_shadowing_also_suppresses_the_shadowed_tables_dependents(reg):
    """Substituting `alpha` must not smuggle the registry's `alpha` back in via `beta`."""
    sql = "WITH alpha AS (SELECT 1 AS x, DATE '2026-01-01' AS d) SELECT * FROM beta"
    order = _injected(sql, reg)
    assert order == ["beta"]


def test_nested_with_inside_a_subquery_shadows_at_top_level_too(reg):
    """KNOWN LIMITATION, asserted as current behaviour rather than called a bug.

    `caller_defined_ctes` scans the whole statement flat, so a `WITH` nested
    inside a subquery marks its name shadowed everywhere — including the
    top-level `FROM alpha`, which then gets no CTE. The result is BigQuery's
    `Unrecognized name: alpha` — a LOUD failure. That is the acceptable side of
    the trade: the alternative (injecting anyway) is a duplicate-CTE error or,
    worse, a silent switch of which definition a subquery reads.
    """
    sql = (
        "SELECT * FROM alpha "
        "JOIN (WITH alpha AS (SELECT 99 AS x) SELECT * FROM alpha) t USING (x)"
    )
    assert resolve_references(sql, reg) == frozenset()
    assert build(sql, reg) == sql  # nothing injected; BigQuery will reject it


# ---------------------------------------------------------------------------
# Injection: what does NOT count as a table reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("SELECT 'FROM alpha' AS label FROM gamma", "single-quoted string literal"),
        ('SELECT "FROM alpha" AS label FROM gamma', "double-quoted STRING literal"),
        ("SELECT '''FROM alpha''' AS label FROM gamma", "triple-quoted string"),
        ("SELECT x AS alpha FROM gamma", "column alias"),
        ("SELECT x FROM gamma -- FROM alpha", "line comment"),
        ("SELECT x FROM gamma /* FROM alpha */", "block comment"),
        ("SELECT x FROM gamma # FROM alpha", "hash comment"),
        ("SELECT x FROM gamma GROUP BY x, alpha", "GROUP BY list comma"),
        (
            "SELECT * FROM `biom-reporting-s26.bpd_raw.alpha`",
            "fully qualified source ref",
        ),
        ("SELECT * FROM bpd_raw.alpha", "unquoted dataset-qualified ref"),
    ],
)
def test_non_table_positions_do_not_trigger_injection(sql, why, reg):
    assert "alpha" not in _injected(sql, reg), why


def test_backtick_quoted_bare_name_is_still_a_reference(reg):
    """`` FROM `alpha` `` is a real CTE reference; only DOTTED backtick names are
    masked out. The `\\b` in `_TABLE_REF` sits inside the alternation for exactly
    this reason — a trailing one would reject the closing backtick."""
    assert _injected("SELECT * FROM `alpha`", reg) == ["alpha"]


def test_comma_separated_table_list_and_joins_are_both_detected(reg):
    assert _injected("SELECT * FROM alpha a, gamma g", reg) == ["alpha", "gamma"]
    assert _injected("SELECT * FROM alpha AS a, gamma AS g", reg) == ["alpha", "gamma"]
    assert _injected("SELECT * FROM alpha, gamma", reg) == ["alpha", "gamma"]
    assert _injected(
        "SELECT * FROM alpha a LEFT JOIN gamma g ON a.x = g.x", reg
    ) == ["alpha", "gamma"]
    assert _injected("SELECT * FROM alpha CROSS JOIN gamma", reg) == ["alpha", "gamma"]


def test_table_list_scan_stops_at_the_first_non_alias_token(reg):
    """The comma-run is anchored to the previous table ref, so a SELECT-list or
    ORDER BY comma downstream can never be mistaken for another table."""
    assert _injected("SELECT x, gamma FROM alpha ORDER BY x, gamma", reg) == ["alpha"]


def test_referenced_logical_tables_is_single_level(reg):
    """The single-level scan sees only what the text names; `resolve_references`
    is what walks it to a fixed point."""
    known = frozenset(reg)
    assert referenced_logical_tables("SELECT * FROM delta", known) == frozenset({"delta"})
    assert resolve_references("SELECT * FROM delta", reg) == frozenset(
        {"alpha", "beta", "delta"}
    )


def test_unknown_names_in_table_position_are_ignored(reg):
    assert _injected("SELECT * FROM some_other_table", reg) == []


# ---------------------------------------------------------------------------
# mask_sql / caller_defined_ctes
# ---------------------------------------------------------------------------


def test_mask_sql_preserves_length_and_newlines():
    """`inject` splices at offsets found in the mask, so lengths must line up."""
    sql = "SELECT 'a b' /* c\nd */ , `p.d.t`, `plain` -- tail\nFROM t\n"
    masked = mask_sql(sql)
    assert len(masked) == len(sql)
    assert masked.count("\n") == sql.count("\n")


def test_mask_sql_blanks_strings_and_comments_but_keeps_bare_backtick_names():
    masked = mask_sql("SELECT 'lit' /*c*/ , `p.d.t`, `plain` -- tail")
    assert "lit" not in masked
    assert "/*c*/" not in masked
    assert "tail" not in masked
    # Dotted backtick refs become non-word filler so `FROM `p.d.t` AS x` cannot
    # leave `AS` looking like a table name.
    assert "`p.d.t`" not in masked
    assert "!!!!!!!" in masked
    # Bare backtick names survive verbatim — they are real references.
    assert "`plain`" in masked


def test_mask_sql_resolves_overlap_left_to_right():
    """A quote inside a comment is not a string, and a comment marker inside a
    string is not a comment. One left-to-right tokenizer is what gets this
    right; sequential regex passes do not."""
    # The apostrophe in "it's" must not open a string that swallows the code
    # following the comment.
    out = mask_sql("/* it's fine */ SELECT real_col")
    assert "fine" not in out
    assert "real_col" in out
    # And a comment marker inside a string is just text; the string ends at its
    # own closing quote, not at a newline or a `*/`.
    out = mask_sql("SELECT 'a -- not a comment', real_col")
    assert "not a comment" not in out
    assert "real_col" in out


def test_caller_defined_ctes_finds_every_member_of_the_list():
    assert caller_defined_ctes(
        "WITH /*x*/ q AS (SELECT 1), `r` AS (SELECT 2), s AS (SELECT 3) SELECT 1"
    ) == frozenset({"q", "r", "s"})


def test_caller_defined_ctes_handles_with_recursive():
    assert caller_defined_ctes("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r") == (
        frozenset({"r"})
    )


def test_caller_defined_ctes_ignores_strings_and_comments():
    assert caller_defined_ctes("SELECT 'WITH q AS (' AS s") == frozenset()
    assert caller_defined_ctes("-- WITH q AS (\nSELECT 1") == frozenset()
    assert caller_defined_ctes("/* WITH q AS ( */ SELECT 1") == frozenset()


# ---------------------------------------------------------------------------
# quote_ident
# ---------------------------------------------------------------------------


def test_quote_ident_emits_backticks_not_double_quotes():
    """WHY this matters, and why it is the highest-priority item in the swap.

    A double-quoted token is a STRING LITERAL in BigQuery, not an identifier:
    `SELECT "sale_quantity"` yields the constant 'sale_quantity'. `SUM("x")`
    errors loudly, but GROUP BY / PARTITION BY / ORDER BY degrade SILENTLY to a
    constant — the ranked CTE in get_inventory_snapshot returned 1 row instead
    of thousands, with no error. The DuckDB-era `"x"` quoting therefore produced
    confidently wrong numbers rather than a failure.
    """
    assert quote_ident("sale_quantity") == "`sale_quantity`"
    assert '"' not in quote_ident("sale_quantity")


def test_quote_ident_does_not_mangle_non_alphanumeric_names():
    """The DuckDB version scrubbed non-alphanumerics to `_`, which silently
    corrupts `biom-reporting-s26` into `biom_reporting_s26`. Inside backticks
    every character is safe, so nothing needs scrubbing."""
    assert quote_ident("biom-reporting-s26") == "`biom-reporting-s26`"
    assert quote_ident("Location Number") == "`Location Number`"
    assert quote_ident("LAST UPDATE DATE") == "`LAST UPDATE DATE`"


def test_quote_ident_rejects_an_embedded_backtick():
    """Rejecting beats mangling: a mangled identifier is a wrong answer, a raised
    error is a bug report."""
    with pytest.raises(ValueError, match="backtick in identifier"):
        quote_ident("bad`name")
    with pytest.raises(ValueError):
        quote_ident("`; DROP TABLE x; --")


# ---------------------------------------------------------------------------
# heuristic_date_column
# ---------------------------------------------------------------------------


def test_heuristic_returns_none_for_no_columns():
    assert heuristic_date_column("anything", []) is None


def test_heuristic_returns_none_when_nothing_fits():
    assert heuristic_date_column("anything", [("name", "STRING"), ("qty", "INT64")]) is None


def test_heuristic_prefers_a_date_typed_column_over_a_date_named_string():
    """Tier 1 beats tier 2 regardless of ordinal position."""
    cols = [("last_remodel_date", "STRING"), ("store_open_date", "DATE")]
    assert heuristic_date_column("location_attr", cols) == "store_open_date"


def test_heuristic_takes_the_earliest_date_typed_column():
    cols = [("qty", "INT64"), ("ts", "TIMESTAMP"), ("d", "DATE")]
    assert heuristic_date_column("x", cols) == "ts"


def test_heuristic_tier_two_matches_date_suffixes():
    assert heuristic_date_column("x", [("qty", "INT64"), ("business_d", "STRING")]) == (
        "business_d"
    )
    assert heuristic_date_column("x", [("run_dt", "STRING")]) == "run_dt"
    assert heuristic_date_column("x", [("load_date", "STRING")]) == "load_date"


def test_heuristic_tier_three_matches_substrings():
    assert heuristic_date_column("x", [("fiscal_week", "STRING")]) == "fiscal_week"
    assert heuristic_date_column("x", [("as_of_stamp", "STRING")]) == "as_of_stamp"
    assert heuristic_date_column("x", [("effective_from", "STRING")]) == "effective_from"


def test_heuristic_consults_column_roles_last(monkeypatch):
    """Tier 4 is unreachable with today's COLUMN_ROLES — every real `date`
    candidate already matches tier 2 or 3 by name — so it is exercised with a
    synthetic role entry. The ordering matters: a generic tier-1..3 hit must win
    over the role list for an unknown table."""
    from bpd_mcp.column_roles import COLUMN_ROLES

    monkeypatch.setitem(COLUMN_ROLES, "synthetic", {"date": ["recorded_on"]})
    cols = [("qty", "INT64"), ("recorded_on", "STRING")]
    assert heuristic_date_column("synthetic", cols) == "recorded_on"
    # Same role list, but now a tier-2 name exists: the heuristic wins.
    cols2 = [("qty", "INT64"), ("recorded_on", "STRING"), ("load_date", "STRING")]
    assert heuristic_date_column("synthetic", cols2) == "load_date"
    # And the role list is not consulted for a table it does not cover.
    assert heuristic_date_column("other", cols) is None


def test_heuristic_role_lookup_is_case_insensitive(monkeypatch):
    from bpd_mcp.column_roles import COLUMN_ROLES

    monkeypatch.setitem(COLUMN_ROLES, "synthetic", {"date": ["recorded_on"]})
    assert heuristic_date_column("synthetic", [("RECORDED_ON", "STRING")]) == "RECORDED_ON"


# ---------------------------------------------------------------------------
# resolve_credentials
# ---------------------------------------------------------------------------

_SA_JSON = b'{"type": "service_account", "project_id": "biom-reporting-s26"}'


@pytest.fixture
def isolated_creds(tmp_path, monkeypatch):
    """Redirect every credential side effect into tmp_path.

    `_SA_KEY_DEST` is computed from `Path.home()` at IMPORT time, so setting
    $HOME alone would not move it — the module constant must be patched too, or
    the test writes to the developer's real ~/.config/gcloud.
    """
    home = tmp_path / "home"
    dest = home / ".config" / "gcloud" / "biom-bq-sa.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(bq, "_SA_KEY_DEST", dest)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SA_KEY_B64", raising=False)
    return dest


@pytest.fixture
def no_adc(monkeypatch):
    """Make google.auth's last-resort ADC lookup fail deterministically.

    Without this the "nothing configured" test would pass or fail depending on
    whether the developer happens to have run `gcloud auth login`.
    """
    import google.auth

    def _boom(*a, **kw):
        raise RuntimeError("no application default credentials in this environment")

    monkeypatch.setattr(google.auth, "default", _boom)


def test_no_credentials_at_all_raises_with_actionable_text(isolated_creds, no_adc):
    with pytest.raises(CredentialsUnavailable) as e:
        resolve_credentials()
    msg = str(e.value)
    # Actionable means: names both env vars, and says what the principal needs.
    assert "GCP_SA_KEY_B64" in msg
    assert "GOOGLE_APPLICATION_CREDENTIALS" in msg
    assert "bigquery.dataViewer" in msg
    assert "bigquery.jobUser" in msg


def test_adc_env_pointing_at_a_missing_file_says_so(isolated_creds, monkeypatch, no_adc):
    """Silently falling through would surface later as ADC's opaque
    'could not determine project'."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/key.json")
    with pytest.raises(CredentialsUnavailable) as e:
        resolve_credentials()
    assert "/nonexistent/key.json" in str(e.value)


def test_existing_adc_env_file_is_used_as_is(isolated_creds, tmp_path, monkeypatch):
    key = tmp_path / "existing.json"
    key.write_bytes(_SA_JSON)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    path, source = resolve_credentials()
    assert path == key
    assert "GOOGLE_APPLICATION_CREDENTIALS" in source
    # Nothing was materialised: the env-var path wins before GCP_SA_KEY_B64.
    assert not isolated_creds.exists()


def test_explicit_path_wins_over_env(isolated_creds, tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_bytes(_SA_JSON)
    other = tmp_path / "other.json"
    other.write_bytes(_SA_JSON)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(other))
    path, source = resolve_credentials(credentials_path=explicit)
    assert path == explicit
    assert source == "explicit credentials_path"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(explicit)


def test_explicit_missing_path_raises(isolated_creds, tmp_path):
    with pytest.raises(CredentialsUnavailable) as e:
        resolve_credentials(credentials_path=tmp_path / "nope.json")
    assert "nope.json" in str(e.value)


def test_sa_key_b64_that_is_not_base64_raises(isolated_creds, monkeypatch):
    monkeypatch.setenv("GCP_SA_KEY_B64", "this is not base64!!!")
    with pytest.raises(CredentialsUnavailable, match="not valid base64"):
        resolve_credentials()
    assert not isolated_creds.exists()


def test_sa_key_b64_that_decodes_to_non_json_raises(isolated_creds, monkeypatch):
    monkeypatch.setenv("GCP_SA_KEY_B64", base64.b64encode(b"not json at all").decode())
    with pytest.raises(CredentialsUnavailable, match="not JSON"):
        resolve_credentials()
    assert not isolated_creds.exists()


def test_sa_key_b64_is_materialised_0600(isolated_creds, monkeypatch):
    dest = isolated_creds
    monkeypatch.setenv("GCP_SA_KEY_B64", base64.b64encode(_SA_JSON).decode())

    path, source = resolve_credentials()

    assert path == dest
    assert dest.read_bytes() == _SA_JSON
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600, "key must never be group/world readable"
    assert "GCP_SA_KEY_B64" in source
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(dest)
    assert list(dest.parent.glob(".sa-key-*")) == [], "temp file leaked"


def test_sa_key_b64_materialisation_is_idempotent(isolated_creds, monkeypatch):
    monkeypatch.setenv("GCP_SA_KEY_B64", base64.b64encode(_SA_JSON).decode())
    first = resolve_credentials()
    # Second call goes down the GOOGLE_APPLICATION_CREDENTIALS branch that the
    # first one set, and must agree on the path.
    second = resolve_credentials()
    assert first[0] == second[0] == isolated_creds
    assert isolated_creds.read_bytes() == _SA_JSON


def test_sa_key_is_published_by_atomic_rename_not_truncate_and_write(
    isolated_creds, monkeypatch
):
    """The exact hazard class this migration removed.

    Every concurrent copy of the server targets this one path. `write_bytes()`
    would truncate the file, create it at the process umask (commonly 0644), and
    only narrow the mode afterwards — so a second server starting concurrently
    could read a half-written or briefly world-readable key. The implementation
    instead writes a `tempfile.mkstemp` file in the SAME directory, fchmods it
    to 0600, writes it in full, and `os.replace`s it into place.

    This asserts the property that makes that safe: at the instant of the
    rename, the source file is already complete and already 0600, and the rename
    is same-directory (so it is atomic, not a cross-device copy).
    """
    dest = isolated_creds
    dest.parent.mkdir(parents=True, exist_ok=True)
    stale = b'{"type": "service_account", "project_id": "STALE"}'
    dest.write_bytes(stale)

    monkeypatch.setenv("GCP_SA_KEY_B64", base64.b64encode(_SA_JSON).decode())

    seen: dict[str, object] = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["src"] = Path(src)
        seen["dst"] = Path(dst)
        seen["src_mode"] = stat.S_IMODE(os.stat(src).st_mode)
        seen["src_bytes"] = Path(src).read_bytes()
        # The destination still holds the OLD key in full — it was never
        # truncated in place.
        seen["dst_bytes_before"] = Path(dst).read_bytes()
        return real_replace(src, dst)

    os.replace = spy
    try:
        resolve_credentials()
    finally:
        os.replace = real_replace

    assert seen, "resolve_credentials did not publish the key via os.replace"
    assert seen["dst"] == dest
    assert seen["src"] != dest, "must write to a temp file, not the destination"
    assert seen["src"].parent == dest.parent, "rename must be same-directory to be atomic"
    assert seen["src_mode"] == 0o600, "temp file must be 0600 BEFORE it is published"
    assert seen["src_bytes"] == _SA_JSON, "temp file must be complete before rename"
    assert seen["dst_bytes_before"] == stale, "destination was truncated in place"
    assert dest.read_bytes() == _SA_JSON
    assert list(dest.parent.glob(".sa-key-*")) == []


def test_failed_write_leaves_no_temp_file_behind(isolated_creds, monkeypatch):
    dest = isolated_creds
    monkeypatch.setenv("GCP_SA_KEY_B64", base64.b64encode(_SA_JSON).decode())

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated failure during publish")

    os.replace = boom
    try:
        with pytest.raises(OSError, match="simulated failure"):
            resolve_credentials()
    finally:
        os.replace = real_replace

    assert not dest.exists()
    assert list(dest.parent.glob(".sa-key-*")) == [], "temp file leaked on failure"


# ---------------------------------------------------------------------------
# The production registry itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LOGICAL_TABLES))
def test_registry_entry_is_well_formed(name):
    t = LOGICAL_TABLES[name]
    body = t.sql.strip()

    assert t.name == name, "registry key must match LogicalTable.name"
    assert body, "empty CTE body"
    assert not body.endswith(";"), (
        "a trailing semicolon inside a CTE body is a syntax error once wrapped in `AS ( ... )`"
    )
    assert t.date_column, "date_column is declared, never probed — it must be set"
    assert t.date_column in t.sql, "declared date_column is not projected by the body"
    assert t.base_tables, "base_tables drives __TABLES__ row counts and cost attribution"
    assert t.primary_base_table == t.base_tables[0]

    for fq in t.base_tables:
        parts = fq.split(".")
        assert len(parts) == 3, f"{fq!r} is not project.dataset.table"
        assert all(parts), f"{fq!r} has an empty component"
        assert f"`{fq}`" in t.sql, (
            f"{fq!r} is declared in base_tables but not referenced backtick-quoted in the body; "
            "an unquoted or missing reference means row counts describe the wrong table"
        )

    for dep in t.depends_on:
        assert dep in LOGICAL_TABLES, f"depends_on names unknown table {dep!r}"

    assert t.date_column in t.sql


@pytest.mark.parametrize("name", sorted(LOGICAL_TABLES))
def test_registry_body_is_self_contained(name):
    """No BPD body references another logical table today, so every single-table
    query must inject exactly ONE CTE. A body that quietly grew a bare-name
    reference would multiply the bytes scanned by every tool that touches it."""
    assert _injected(f"SELECT * FROM {name}", LOGICAL_TABLES) == [name]


def test_registry_uses_the_column_roles_spelling():
    """`item_attr` / `location_attr`, not `items` / `locations` — DATASET_KINDS,
    FEED_KINDS, DATE_RANGE_ROLES and schemas.KnownDataset all key off these."""
    assert "item_attr" in LOGICAL_TABLES
    assert "location_attr" in LOGICAL_TABLES
    assert "items" not in LOGICAL_TABLES
    assert "locations" not in LOGICAL_TABLES


def test_known_dataset_names_tracks_the_registry():
    assert tuple(LOGICAL_TABLES) == bq.KNOWN_DATASET_NAMES
    assert bq.logical_names() == frozenset(LOGICAL_TABLES)


def test_base_datasets_are_the_two_source_datasets():
    assert bq.base_datasets() == frozenset({"biom_canvas", "bpd_raw"})


def test_pattern_to_logical_inverts_patterns():
    inverse = bq.pattern_to_logical()
    for name, t in LOGICAL_TABLES.items():
        for pattern in t.patterns:
            assert name in inverse[pattern]
    for pattern, names in inverse.items():
        for name in names:
            assert pattern in LOGICAL_TABLES[name].patterns


def test_get_raises_a_helpful_keyerror():
    with pytest.raises(KeyError) as e:
        bq.get("sales")
    assert "unknown logical table" in str(e.value)
    assert "sales_daily" in str(e.value), "the error must list the known names"


def test_latest_state_note_is_present_exactly_where_a_dedup_is():
    """`describe()` renders this note so a reduction is never invisible to a
    caller comparing against raw row counts. The two entries carrying a QUALIFY
    are the two that must have it."""
    with_qualify = {n for n, t in LOGICAL_TABLES.items() if "QUALIFY" in t.sql.upper()}
    with_note = {n for n, t in LOGICAL_TABLES.items() if t.latest_state_note}
    assert with_qualify == with_note
    assert with_qualify == {"orders_daily", "forecast_weekly"}


def test_po_plan_tables_carry_no_registry_level_dedup():
    """get_upcoming_pos owns the MAX(business_d) reduction for these two. A
    QUALIFY here would double-apply it, silently."""
    for name in ("po_plan_daily", "po_plan_biweekly"):
        assert "QUALIFY" not in LOGICAL_TABLES[name].sql.upper()
        assert LOGICAL_TABLES[name].latest_state_note is None


def test_build_against_the_real_registry_injects_only_what_is_asked_for():
    """forecast_weekly alone is 151 MB of scan and cannot be pruned — it must
    never ride along with an unrelated query."""
    sql, order = build_with_report(
        "SELECT s.tcin, SUM(s.sale_quantity) FROM sales_daily s "
        "JOIN item_attr i ON s.tcin = i.tcin GROUP BY 1"
    )
    assert order == ["item_attr", "sales_daily"]
    assert "forecast_weekly" not in sql
    assert "dfe_wkly_item_loc_forecast" not in sql
    assert sql.startswith("WITH item_attr AS (")
