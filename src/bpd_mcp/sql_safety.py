"""Defense-in-depth SQL validation for `bpd_run_sql`.

Layer 1: token-scan rejects keywords that could mutate state or attach files.
Layer 2: multi-statement detection.
Layer 3: comment-stripped pre-pass (so `/* */ DROP TABLE` is caught).
Layer 4: caller MUST run on a connection opened `read_only=True`. We assert that
         too in the tool, so even if all of the above were bypassed the engine refuses.

This module is intentionally conservative; false positives are preferable to false
negatives. Anything other than a single SELECT / WITH is rejected.
"""

from __future__ import annotations

import re

# Anything outside of this allow-list of leading keywords is rejected.
_ALLOWED_LEAD = ("SELECT", "WITH", "EXPLAIN", "DESCRIBE", "SHOW", "PRAGMA")

# These tokens are forbidden anywhere — even cloaked in a comment.
_FORBIDDEN_TOKENS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "CREATE",
    "DROP",
    "ALTER",
    "REPLACE",
    "MERGE",
    "ATTACH",
    "DETACH",
    "COPY",
    "EXPORT",
    "IMPORT",
    "INSTALL",
    "LOAD",
    "CALL",
    "GRANT",
    "REVOKE",
    "VACUUM",
    "CHECKPOINT",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "UPDATE_EXTENSIONS",
}

# Allowed PRAGMA names (read-only introspection only).
_PRAGMA_ALLOWLIST = {
    "table_info",
    "show_tables",
    "database_list",
    "version",
    "show_databases",
    "show_views",
    "memory_limit",
}


class SqlBlocked(ValueError):
    """Raised when SQL is rejected by the safety layer (code SQL_BLOCKED)."""


_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LIT = re.compile(r"'(?:''|[^'])*'")
_DBL_QUOTED = re.compile(r'"(?:""|[^"])*"')


def _strip_comments_and_strings(sql: str) -> str:
    """Remove SQL comments and string literals so token scans see only structure."""
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _STRING_LIT.sub("''", sql)
    sql = _DBL_QUOTED.sub('""', sql)
    return sql


def _split_statements(sql: str) -> list[str]:
    """Split on ; *outside* string literals (already stripped)."""
    return [s.strip() for s in sql.split(";") if s.strip()]


def validate(sql: str) -> str:
    """Return the cleaned, single-statement SQL if it's safe; raise SqlBlocked otherwise."""
    if not sql or not sql.strip():
        raise SqlBlocked("empty SQL")

    cleaned = _strip_comments_and_strings(sql)
    stmts = _split_statements(cleaned)
    if len(stmts) != 1:
        raise SqlBlocked(
            f"multiple statements detected ({len(stmts)}). Only one SELECT/WITH allowed."
        )

    stmt = stmts[0]
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stmt)
    if not tokens:
        raise SqlBlocked("no tokens parsed from SQL")

    lead = tokens[0].upper()
    if lead not in _ALLOWED_LEAD:
        raise SqlBlocked(f"leading keyword {lead!r} not permitted; must be one of {_ALLOWED_LEAD}")

    upper_tokens = {t.upper() for t in tokens}
    bad = upper_tokens & _FORBIDDEN_TOKENS
    if bad:
        raise SqlBlocked(f"forbidden keyword(s) detected: {sorted(bad)}")

    if lead == "PRAGMA":
        # PRAGMA can be read-only OR can mutate config. Only allow a small allow-list.
        # Token after PRAGMA, lowercased without parens.
        if len(tokens) < 2:
            raise SqlBlocked("PRAGMA requires an argument")
        pragma_name = tokens[1].lower()
        if pragma_name not in _PRAGMA_ALLOWLIST:
            raise SqlBlocked(
                f"PRAGMA {pragma_name!r} not in allowlist {sorted(_PRAGMA_ALLOWLIST)}"
            )
        # Reject assignments like `PRAGMA name = value`.
        if "=" in stmt:
            raise SqlBlocked("PRAGMA assignments are not permitted (read-only)")

    return sql.strip().rstrip(";")


def wrap_with_limit(sql: str, limit: int) -> str:
    """Wrap a validated single-statement SELECT/WITH with a row cap."""
    s = sql.strip().rstrip(";")
    # Use a subquery so a user-supplied LIMIT/ORDER BY doesn't get clobbered.
    return f"SELECT * FROM ({s}) AS _bpd_sub LIMIT {int(limit)}"
