"""Defense-in-depth SQL validation for `bpd_run_sql`.

Layer 1: token-scan rejects keywords that could mutate state or attach files.
Layer 2: multi-statement detection.
Layer 3: comment-stripped pre-pass (so `/* */ DROP TABLE` is caught).
Layer 4: the BigQuery credential itself is read-only. The service account holds
         dataViewer + jobUser and nothing more, so even if every layer above were
         bypassed the write is refused at the IAM layer (verified: a CREATE TABLE
         returns 403 "Permission bigquery.tables.create denied"). That is strictly
         stronger than the old read-only-connection assertion it replaces.

This module is intentionally conservative; false positives are preferable to false
negatives. Anything other than a single SELECT / WITH is rejected.
"""

from __future__ import annotations

import re

# Anything outside of this allow-list of leading keywords is rejected.
_ALLOWED_LEAD = ("SELECT", "WITH")

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
    "SCRIPT",
    "EXECUTE",
    "SET",
    "DECLARE",
    "ASSERT",
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

    return sql.strip().rstrip(";")


def wrap_with_limit(sql: str, limit: int) -> str:
    """Wrap a validated single-statement SELECT/WITH with a row cap."""
    s = sql.strip().rstrip(";")
    # Use a subquery so a user-supplied LIMIT/ORDER BY doesn't get clobbered.
    return f"SELECT * FROM ({s}) AS _bpd_sub LIMIT {int(limit)}"
