"""structlog wiring with stderr-only console output and a rotating JSON file handler.

Critical for stdio MCP servers: stdout is reserved for protocol messages. All log
output and any stray prints must go to stderr or to a file. This module enforces that.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

import structlog

_SECRET_KEY_PATTERN = re.compile(r"(?i)(password|secret|token|authorization|bearer|refresh)")


def _mask_token_value(value: str) -> str:
    """Render a token as `<token:1234...abcd>` keeping first/last 4."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if len(s) <= 12:
        return "<token:redacted>"
    return f"<token:{s[:4]}...{s[-4:]}>"


def _redact_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Recursively redact any key matching the secret pattern."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for k, v in node.items():
                if isinstance(k, str) and _SECRET_KEY_PATTERN.search(k):
                    out[k] = _mask_token_value(v) if isinstance(v, str) else "<redacted>"
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(event_dict)


_configured = False


def configure_logging(log_level: str, log_dir: Path) -> None:
    """Configure structlog + stdlib logging once. Idempotent."""
    global _configured
    if _configured:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bpd-mcp.log"

    level = getattr(logging, log_level.upper(), logging.INFO)

    # stdlib root logger: stderr console + rotating file. NEVER stdout.
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(level)

    fmt = logging.Formatter("%(message)s")
    stderr_handler.setFormatter(fmt)
    file_handler.setFormatter(fmt)

    root.addHandler(stderr_handler)
    root.addHandler(file_handler)

    # Silence overly chatty third-parties at INFO.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def mask_token(value: str | None) -> str:
    """Public helper that mirrors the redactor for use in non-log contexts."""
    if value is None:
        return "<none>"
    return _mask_token_value(value)
