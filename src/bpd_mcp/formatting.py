"""Render tool outputs as markdown or JSON. Used by every tool.

Keep tables narrow — markdown tables wider than ~8 columns become unreadable for Claude.
For wide rows, switch to a key/value rendering.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from .schemas import ToolResponse


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    if isinstance(o, set | frozenset):
        return sorted(o)
    return str(o)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime | date):
        return v.isoformat()
    s = str(v).replace("|", "\\|").replace("\n", " ").strip()
    if len(s) > 80:
        s = s[:77] + "..."
    return s


def render_markdown_table(rows: list[dict[str, Any]], *, columns: list[str] | None = None) -> str:
    if not rows:
        return "_(no rows)_"
    cols = columns or list(rows[0].keys())
    header = "| " + " | ".join(cols) + " |"
    divider = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [header, divider]
    for r in rows:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def render_keyvalue(d: dict[str, Any]) -> str:
    return "\n".join(f"- **{k}**: {_cell(v)}" for k, v in d.items())


def make_kv_response(
    *,
    data: dict[str, Any],
    title: str | None = None,
    fmt: str = "markdown",
) -> ToolResponse:
    if fmt == "json":
        return ToolResponse(
            ok=True,
            format="json",
            rendered=json.dumps(data, default=_json_default, indent=2),
            data=data,
        )
    body = ""
    if title:
        body += f"### {title}\n\n"
    body += render_keyvalue(data)
    return ToolResponse(ok=True, format="markdown", rendered=body, data=data)


def make_table_response(
    *,
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
    title: str | None = None,
    fmt: str = "markdown",
    extra: dict[str, Any] | None = None,
) -> ToolResponse:
    data = {"rows": rows, "row_count": len(rows)}
    if extra:
        data.update(extra)
    if fmt == "json":
        return ToolResponse(
            ok=True,
            format="json",
            rendered=json.dumps(data, default=_json_default, indent=2),
            data=data,
        )
    body = ""
    if title:
        body += f"### {title}\n\n"
    body += render_markdown_table(rows, columns=columns)
    return ToolResponse(ok=True, format="markdown", rendered=body, data=data)


def make_error_response(
    *, code: str, message: str, details: dict[str, Any] | None = None, fmt: str = "markdown"
) -> ToolResponse:
    err = {"code": code, "message": message, "details": details or {}}
    if fmt == "json":
        return ToolResponse(
            ok=False,
            format="json",
            rendered=json.dumps({"error": err}, default=_json_default, indent=2),
            error=err,  # type: ignore[arg-type]
        )
    return ToolResponse(
        ok=False,
        format="markdown",
        rendered=f"**Error {code}**: {message}",
        error=err,  # type: ignore[arg-type]
    )
