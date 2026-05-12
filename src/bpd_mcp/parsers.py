"""BPD filename → (dataset, file_date, parse_options) catalog and the file parser.

The actual schemas are NOT known up front — we read whatever header Target ships
and persist the discovered schema in `_schema_registry`. The catalog below tells us
which DuckDB table a file belongs to and what its natural primary key is.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import polars as pl

from .logging_setup import get_logger

logger = get_logger(__name__)


class ParseError(RuntimeError):
    """Raised when a file looks valid by name but its content can't be parsed."""


Dataset = Literal[
    "sales_daily",
    "inventory_daily",
    "sales_weekly",
    "inventory_weekly",
    "item_attr",
    "location_attr",
    "gross_margin",
]


@dataclass(frozen=True)
class FilePattern:
    """One row in the BPD filename catalog (§6)."""

    dataset: Dataset
    regex: re.Pattern[str]
    granularity: str
    frequency: str
    # Natural primary key candidates, in priority order. We normalize header → snake_case
    # then pick the first set that fully matches the discovered columns.
    primary_key_candidates: tuple[tuple[str, ...], ...]


# Catalog -----------------------------------------------------------------------------
#
# Pattern conventions per §6:
#   {TIER}_{BPID}_..._{MMDDYYYY}_KW.zip
#   {TIER} = BV | BR | CC
#   {BPID} = digits
#   MMDDYYYY = file generation date

_TIER_RE = r"(?P<tier>BV|BR|CC)"
_BPID_RE = r"(?P<bpid>\d+)"
_DATE_RE = r"(?P<date>\d{8})"
_KW = r"_KW\.zip$"


def _pat(body: str) -> re.Pattern[str]:
    return re.compile(rf"^{_TIER_RE}_{_BPID_RE}_{body}_{_DATE_RE}{_KW}", re.IGNORECASE)


PATTERNS: tuple[FilePattern, ...] = (
    FilePattern(
        dataset="sales_daily",
        regex=_pat(r"DLY_SALES_ITEM_LOC_VEND"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=(
            ("tcin", "location_id", "sale_date"),
            ("tcin", "store_id", "sale_date"),
            ("tcin", "loc_id", "sale_date"),
        ),
    ),
    FilePattern(
        dataset="inventory_daily",
        regex=_pat(r"DLY_INV_ITEM_LOC_VEND"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=(
            ("tcin", "location_id", "snapshot_date"),
            ("tcin", "store_id", "snapshot_date"),
            ("tcin", "loc_id", "snapshot_date"),
        ),
    ),
    FilePattern(
        dataset="sales_weekly",
        regex=_pat(r"WKLY_SALES_ITEM_LOC_VEND"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=(
            ("tcin", "location_id", "week_end_date"),
            ("tcin", "store_id", "week_end_date"),
            ("tcin", "loc_id", "week_end_date"),
        ),
    ),
    FilePattern(
        dataset="inventory_weekly",
        regex=_pat(r"WKLY_INV_ITEM_LOC_VEND"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=(
            ("tcin", "location_id", "week_end_date"),
            ("tcin", "store_id", "week_end_date"),
        ),
    ),
    FilePattern(
        dataset="item_attr",
        regex=_pat(r"WKLY_ITM_VEND_ATTR"),
        granularity="item",
        frequency="weekly",
        primary_key_candidates=(("tcin",), ("dpci",)),
    ),
    FilePattern(
        dataset="gross_margin",
        regex=_pat(r"WKLY_GM_ITEM_VEND"),
        granularity="item × week",
        frequency="weekly",
        primary_key_candidates=(
            ("tcin", "week_end_date"),
            ("tcin", "fiscal_week"),
        ),
    ),
    # location_attr is special — it's `ALL_WKLY_LOC_ATTR_V0_0_{date}_KW.zip` (no tier, no bpid).
    FilePattern(
        dataset="location_attr",
        regex=re.compile(
            r"^ALL_WKLY_LOC_ATTR_V0_0_(?P<date>\d{8})_KW\.zip$", re.IGNORECASE
        ),
        granularity="location",
        frequency="weekly",
        primary_key_candidates=(("location_id",), ("store_id",), ("loc_id",)),
    ),
)


@dataclass(frozen=True)
class ParsedFilename:
    pattern: FilePattern
    file_date: date
    tier: str | None
    bpid: str | None


def classify_filename(name: str) -> ParsedFilename | None:
    """Match a Kiteworks file name against the catalog. Returns None if unknown."""
    base = Path(name).name
    for pat in PATTERNS:
        m = pat.regex.match(base)
        if not m:
            continue
        date_str = m.group("date")
        try:
            file_date = datetime.strptime(date_str, "%m%d%Y").date()
        except ValueError:
            continue
        gd = m.groupdict()
        return ParsedFilename(
            pattern=pat,
            file_date=file_date,
            tier=gd.get("tier"),
            bpid=gd.get("bpid"),
        )
    return None


# Reading -----------------------------------------------------------------------------


def _normalize_column_name(name: str) -> str:
    """Header→snake_case. Strip BOM, whitespace, punctuation; collapse runs."""
    s = name.strip().lstrip("﻿")
    s = re.sub(r"[^0-9A-Za-z]+", "_", s)
    s = s.strip("_").lower()
    return s or "col"


def _sniff_delimiter(sample: bytes) -> str:
    """Look at the first non-empty line; pick from \\t | , in that priority order."""
    head = sample.splitlines()
    line = next(
        (bytes(ln).decode("utf-8", errors="replace") for ln in head if ln.strip()), ""
    )
    counts = {d: line.count(d) for d in ("\t", "|", ",")}
    best = max(counts, key=counts.get)  # type: ignore[arg-type]
    return best if counts[best] > 0 else "|"


def open_zipped_text(zip_path: Path) -> tuple[str, bytes]:
    """Return (inner_filename, raw_bytes) of the first text member inside the zip."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if not members:
            raise ParseError(f"{zip_path.name}: zip contains no files")
        # Prefer one that doesn't look like a checksum/sidecar.
        text_members = [
            m
            for m in members
            if not m.filename.lower().endswith((".md5", ".sha", ".sha256", ".sig"))
        ]
        chosen = text_members[0] if text_members else members[0]
        with zf.open(chosen) as f:
            data = f.read()
    return chosen.filename, data


def read_dataframe(zip_path: Path) -> tuple[pl.DataFrame, list[str], str]:
    """Parse a BPD zip into a Polars DataFrame.

    Returns (df, original_columns, delimiter).
    """
    inner_name, raw = open_zipped_text(zip_path)
    delim = _sniff_delimiter(raw[:64 * 1024])
    try:
        df = pl.read_csv(
            io.BytesIO(raw),
            separator=delim,
            has_header=True,
            infer_schema_length=10000,
            null_values=["", "NULL", "null"],
            try_parse_dates=False,
            truncate_ragged_lines=True,
            ignore_errors=False,
        )
    except Exception as e:
        # Surface the first 5 problem-looking lines to aid debugging.
        sample_lines = raw.splitlines()[:6]
        preview = b"\n".join(sample_lines).decode("utf-8", errors="replace")
        raise ParseError(
            f"{zip_path.name} (inner: {inner_name}): polars read failed ({e}).\n"
            f"delim={delim!r}\nfirst lines:\n{preview}"
        ) from e

    original = list(df.columns)
    df = df.rename({c: _normalize_column_name(c) for c in df.columns})
    df = _cast_known_columns(df)
    return df, original, delim


# ---------- type casting ----------

_DATE_HINTS = re.compile(r"(?:^|_)(date|dt)$|^(?:sale|snapshot|week_end|week_start)_date$")
_INT_HINTS = re.compile(
    r"^(tcin|dpci|location_id|store_id|loc_id|fiscal_week|week|units?(_sold)?|qty|count|inv_units?)$"
)
_FLOAT_HINTS = re.compile(
    r"(_amt|_amount|_dollars?|_sales|_cost|_price|_margin|_pct|_rate)$|^gm$|^gross_margin$"
)


def _cast_known_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Apply lightweight type casts based on column-name heuristics.

    `-1` sentinels are preserved (we keep ints as ints; no NULL coercion).
    """
    exprs: list[pl.Expr] = []
    for name, dtype in df.schema.items():
        col = pl.col(name)
        if _DATE_HINTS.search(name):
            exprs.append(col.cast(pl.String).str.strptime(pl.Date, format="%Y-%m-%d", strict=False).alias(name))
        elif _INT_HINTS.match(name) and not dtype.is_integer():
            exprs.append(col.cast(pl.Int64, strict=False).alias(name))
        elif _FLOAT_HINTS.search(name) and dtype != pl.Float64:
            exprs.append(col.cast(pl.Float64, strict=False).alias(name))
    if exprs:
        df = df.with_columns(exprs)
    return df


def derive_duckdb_schema(df: pl.DataFrame) -> dict[str, str]:
    """Map a Polars schema to {col_name: duckdb_type}."""
    return {name: _polars_to_duckdb(dtype) for name, dtype in df.schema.items()}


def _polars_to_duckdb(dtype: pl.DataType) -> str:
    if dtype.is_integer():
        return "BIGINT"
    if dtype.is_float():
        return "DOUBLE"
    if dtype == pl.Boolean:
        return "BOOLEAN"
    if dtype == pl.Date:
        return "DATE"
    if dtype == pl.Datetime:
        return "TIMESTAMP"
    return "TEXT"
