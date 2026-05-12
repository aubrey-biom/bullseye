"""BPD filename → (dataset, file_date, parse_options) catalog and the file parser.

The actual schemas are NOT known up front — we read whatever header Target ships
and persist the discovered schema in `_schema_registry`. The catalog below tells us
which DuckDB table a file belongs to and what its natural primary key is.

§ Filename catalog (patched May 2026):

Real Target filenames are *not* the ones in the original spec PDF — Target's BPD
delivery has evolved. The catalog below matches the names Target ships today.
Target is inconsistent across dataset types: some use `DAILY` / `WEEKLY`, others
use `DLY` / `WKLY`, and the bi-weekly PO planning file uses `BI_WEEKLY`. Each
regex below encodes the specific spelling Target uses for that dataset; we do not
mass-alias `DAILY`↔`DLY` because that would risk two patterns matching the same
file.

`WEEKLY_<METRIC>_TCIN_LOC_<DATE>` vs `WEEKLY_<METRIC>_TCIN_<DATE>` distinguishes
the item × location × week file from the item × week rollup. The regex anchors
`_<DATE>_KW.zip` at the end, so the two never collide.
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
    # Sales & inventory (item × location)
    "sales_daily",
    "sales_weekly",
    "inventory_daily",
    "inventory_weekly",
    # Sales & inventory (item-only rollup, no location)
    "sales_weekly_item",
    "inventory_weekly_item",
    # Margin
    "gross_margin",
    "gross_margin_item",
    # Item dimension
    "item_attr",
    "item_attr_extended",
    # Location dimension
    "location_attr",
    # NEW (May 2026 patch): orders, PO planning, demand forecast
    "orders_daily",
    "po_plan_daily",
    "po_plan_biweekly",
    "forecast_weekly",
]


@dataclass(frozen=True)
class FilePattern:
    """One row in the BPD filename catalog."""

    dataset: Dataset
    regex: re.Pattern[str]
    granularity: str
    frequency: str
    # Natural primary key candidates, in priority order. We normalize header → snake_case
    # then pick the first set that fully matches the discovered columns.
    primary_key_candidates: tuple[tuple[str, ...], ...]


# --- Filename building blocks ---------------------------------------------------------

_TIER_RE = r"(?P<tier>BV|BR|CC)"
_BPID_RE = r"(?P<bpid>\d+)"
_DATE_RE = r"(?P<date>\d{8})"
_KW = r"_KW\.zip$"


def _pat(body: str) -> re.Pattern[str]:
    """`^TIER_BPID_<body>_<DATE>_KW.zip$` — used by every tier/BPID-scoped pattern."""
    return re.compile(rf"^{_TIER_RE}_{_BPID_RE}_{body}_{_DATE_RE}{_KW}", re.IGNORECASE)


def _bare(body: str) -> re.Pattern[str]:
    """`^<body>_<DATE>_KW.zip$` — used by tier-less, BPID-less files (location_attr)."""
    return re.compile(rf"^{body}_{_DATE_RE}{_KW}", re.IGNORECASE)


# Common location-id candidate columns Target's data uses, in priority order.
_LOC_COLS = ("location_id", "store_id", "loc_id", "store_nbr", "location_nbr")


def _pk_with_loc(date_col: str) -> tuple[tuple[str, ...], ...]:
    return tuple(("tcin", lc, date_col) for lc in _LOC_COLS)


def _pk_item(date_col: str) -> tuple[tuple[str, ...], ...]:
    return (("tcin", date_col),)


# --- Pattern catalog ------------------------------------------------------------------
#
# Order: more-specific patterns FIRST so they win on first match. Regex anchoring at
# `_<DATE>_KW.zip$` keeps collisions rare in practice — the `_LOC_` variants don't
# collide with their non-`_LOC_` siblings because the `_LOC_` shifts the date offset.

PATTERNS: tuple[FilePattern, ...] = (
    # ---------- sales (item × location × day | week) ----------
    FilePattern(
        dataset="sales_daily",
        regex=_pat(r"DAILY_SALES_TCIN_LOC"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=_pk_with_loc("sale_date"),
    ),
    FilePattern(
        dataset="sales_weekly",
        regex=_pat(r"WEEKLY_SALES_TCIN_LOC"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc("week_end_date"),
    ),
    FilePattern(
        dataset="sales_weekly_item",
        regex=_pat(r"WEEKLY_SALES_TCIN"),
        granularity="item × week (rolled up across locations)",
        frequency="weekly",
        primary_key_candidates=_pk_item("week_end_date"),
    ),

    # ---------- inventory (item × location × day | week) ----------
    FilePattern(
        dataset="inventory_daily",
        regex=_pat(r"DAILY_INV_TCIN_LOC"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=(
            *_pk_with_loc("snapshot_date"),
            *_pk_with_loc("inv_date"),
            *_pk_with_loc("inventory_date"),
        ),
    ),
    FilePattern(
        dataset="inventory_weekly",
        regex=_pat(r"WEEKLY_INV_TCIN_LOC"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc("week_end_date"),
    ),
    FilePattern(
        dataset="inventory_weekly_item",
        regex=_pat(r"WEEKLY_INV_TCIN"),
        granularity="item × week (rolled up across locations)",
        frequency="weekly",
        primary_key_candidates=_pk_item("week_end_date"),
    ),

    # ---------- gross margin ----------
    FilePattern(
        dataset="gross_margin",
        regex=_pat(r"WEEKLY_GM_TCIN_LOC"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc("week_end_date"),
    ),
    FilePattern(
        dataset="gross_margin_item",
        regex=_pat(r"WEEKLY_GM_TCIN"),
        granularity="item × week (rolled up across locations)",
        frequency="weekly",
        primary_key_candidates=_pk_item("week_end_date"),
    ),

    # ---------- item dimension ----------
    # The brief notes two filename variants probably point to the same logical data.
    # We can't verify without inspecting real headers, so we default to SEPARATE tables
    # to avoid silent column-loss on schema drift. If real headers turn out identical,
    # consolidating is a `CREATE TABLE item_attr_consolidated AS SELECT * FROM ...`
    # away. The brief explicitly permitted this fallback.
    FilePattern(
        dataset="item_attr",
        regex=_pat(r"WEEKLY_ITEM_MTA"),
        granularity="item",
        frequency="weekly",
        primary_key_candidates=(("tcin",), ("dpci",), ("tcin", "fiscal_week")),
    ),
    FilePattern(
        dataset="item_attr_extended",
        regex=_pat(r"WKLY_TCIN_ITEM"),
        granularity="item",
        frequency="weekly",
        primary_key_candidates=(("tcin",), ("dpci",), ("tcin", "fiscal_week")),
    ),

    # ---------- location dimension (shared file: no tier, no BPID) ----------
    FilePattern(
        dataset="location_attr",
        regex=_bare(r"ALL_WKLY_LOC_ATTR_V0_0"),
        granularity="location",
        frequency="weekly",
        primary_key_candidates=tuple((lc,) for lc in _LOC_COLS),
    ),

    # ---------- orders (May 2026 patch) ----------
    # Target's purchase orders to the vendor. Schema unknown at code-write time; we
    # discover columns from the first real header and pick whichever PK actually
    # exists in the data.
    FilePattern(
        dataset="orders_daily",
        regex=_pat(r"DAILY_ORDER_TCIN_LOC"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=(
            # Most likely (per brief): order is keyed by PO + line.
            ("po_number", "tcin", "location_id"),
            ("po_nbr", "tcin", "location_id"),
            ("po_id", "tcin", "location_id"),
            # Fallback: date × item × location (loses individual PO identity but
            # is at least granular enough to be idempotent).
            *_pk_with_loc("order_date"),
            *_pk_with_loc("po_date"),
        ),
    ),

    # ---------- PO planning (May 2026 patch) ----------
    # Target's forward-looking PO plan. The "daily" file is item-grain (no _LOC_).
    FilePattern(
        dataset="po_plan_daily",
        regex=_pat(r"DLY_PO_PLAN_TCIN"),
        granularity="item × day",
        frequency="daily",
        primary_key_candidates=(
            ("tcin", "plan_date"),
            ("tcin", "po_date"),
            ("tcin", "expected_date"),
            ("tcin", "week_end_date"),
        ),
    ),
    FilePattern(
        dataset="po_plan_biweekly",
        regex=_pat(r"BI_WEEKLY_PO_PLANNING"),
        granularity="item × bi-weekly period",
        frequency="bi-weekly",
        primary_key_candidates=(
            ("tcin", "period_start_date"),
            ("tcin", "period_end_date"),
            ("tcin", "week_end_date"),
            ("tcin", "plan_date"),
        ),
    ),

    # ---------- demand forecast (May 2026 patch) ----------
    # DFE = Target's "Demand Forecast Engine". Their internal forecast of vendor SKU
    # sell-through by store-week. Critical for S&OP.
    FilePattern(
        dataset="forecast_weekly",
        regex=_pat(r"DFE_WKLY_ITEM_LOC_FORECAST"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc("week_end_date"),
    ),
)


@dataclass(frozen=True)
class ParsedFilename:
    pattern: FilePattern
    file_date: date
    tier: str | None
    bpid: str | None


def _parse_date(date_str: str) -> date | None:
    """Try MMDDYYYY first (BPD historical convention), fall back to YYYYMMDD.

    Target's docs say MMDDYYYY but their on-the-wire usage has drifted; YYYYMMDD
    sometimes shows up in newer files. Both are 8 digits, so we try the format
    that the original spec called out first and fall back to the ISO-ish form.
    """
    for fmt in ("%m%d%Y", "%Y%m%d", "%d%m%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def classify_filename(name: str) -> ParsedFilename | None:
    """Match a Kiteworks file name against the catalog. Returns None if unknown."""
    base = Path(name).name
    for pat in PATTERNS:
        m = pat.regex.match(base)
        if not m:
            continue
        date_str = m.group("date")
        file_date = _parse_date(date_str)
        if file_date is None:
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

_DATE_HINTS = re.compile(
    r"(?:^|_)(date|dt)$"
    r"|^(sale|snapshot|week_end|week_start|order|po|plan|expected|period_start|period_end|forecast|inv|inventory)_date$"
)
_INT_HINTS = re.compile(
    # Exact tokens that are integers across BPD datasets.
    r"^(tcin|dpci|location_id|store_id|loc_id|store_nbr|location_nbr|fiscal_week|week)$"
    # Suffix-style int hints: any *_qty, *_units, *_count, on_hand_units, etc.
    r"|_qty$|_units?$|_count$|_nbr$"
    r"|^units?(_sold)?$|^count$|^inv_units?$|^on_hand(_units?)?$"
    r"|^planned_units?$|^forecast_units?$|^order_units?$"
)
_FLOAT_HINTS = re.compile(
    r"(_amt|_amount|_dollars?|_sales|_cost|_price|_margin|_pct|_rate)$"
    r"|^gm$|^gross_margin$|^(forecast|actual|variance)_dollars?$"
)


def _cast_known_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Apply lightweight type casts based on column-name heuristics.

    `-1` sentinels are preserved (we keep ints as ints; no NULL coercion).
    """
    exprs: list[pl.Expr] = []
    for name, dtype in df.schema.items():
        col = pl.col(name)
        if _DATE_HINTS.search(name):
            exprs.append(
                col.cast(pl.String)
                .str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
                .alias(name)
            )
        elif _INT_HINTS.search(name) and not dtype.is_integer():
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
