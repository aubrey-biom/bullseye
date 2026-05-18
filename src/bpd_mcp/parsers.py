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
# `location_number` was added in Patch #6.2.1 — it's the real column name in
# `location_attr` and possibly other dimension-style files. `location_id` stays
# first because every fact table (sales_*, inventory_*, etc.) ships that name.
_LOC_COLS = (
    "location_id", "location_number", "store_id", "loc_id",
    "store_nbr", "location_nbr",
)


def _pk_with_loc(*date_cols: str) -> tuple[tuple[str, ...], ...]:
    """PK candidates as a cartesian product: TCIN × LOC_COLS × date_cols.

    `date_cols` are tried in the order given — `_pick_primary_key` picks the
    first candidate whose columns all exist in the df. List the real Target
    column name first; aliases are fallbacks for older fixture shapes (Patch
    #6.2: prevents silent DELETE-skip when Target ships `sales_date` but the
    catalog only knew about `week_end_date`).
    """
    return tuple(("tcin", lc, dc) for dc in date_cols for lc in _LOC_COLS)


def _pk_item(*date_cols: str) -> tuple[tuple[str, ...], ...]:
    """Item-only PK candidates: TCIN × date_cols (no location dimension)."""
    return tuple(("tcin", dc) for dc in date_cols)


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
        primary_key_candidates=_pk_with_loc(
            "sales_date", "sale_date", "transaction_date"
        ),
    ),
    FilePattern(
        dataset="sales_weekly",
        regex=_pat(r"WEEKLY_SALES_TCIN_LOC"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc(
            "sales_date", "week_end_date", "fiscal_week_end_d",
            "fiscal_week_end_date", "sale_date",
        ),
    ),
    FilePattern(
        dataset="sales_weekly_item",
        regex=_pat(r"WEEKLY_SALES_TCIN"),
        granularity="item × week (rolled up across locations)",
        frequency="weekly",
        primary_key_candidates=_pk_item(
            "sales_date", "week_end_date", "fiscal_week_end_d",
            "fiscal_week_end_date", "sale_date",
        ),
    ),

    # ---------- inventory (item × location × day | week) ----------
    FilePattern(
        dataset="inventory_daily",
        regex=_pat(r"DAILY_INV_TCIN_LOC"),
        granularity="item × location × day",
        frequency="daily",
        primary_key_candidates=_pk_with_loc(
            "report_date_dim", "inventory_date", "snapshot_date",
            "inv_date", "as_of_date",
        ),
    ),
    FilePattern(
        dataset="inventory_weekly",
        regex=_pat(r"WEEKLY_INV_TCIN_LOC"),
        granularity="item × location × week",
        frequency="weekly",
        primary_key_candidates=_pk_with_loc(
            "report_date_dim", "week_end_date", "fiscal_week_end_d",
            "inventory_date", "snapshot_date",
        ),
    ),
    FilePattern(
        dataset="inventory_weekly_item",
        regex=_pat(r"WEEKLY_INV_TCIN"),
        granularity="item × week (rolled up across locations)",
        frequency="weekly",
        primary_key_candidates=_pk_item(
            "report_date_dim", "week_end_date", "fiscal_week_end_d",
            "inventory_date",
        ),
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
            # Real Target column names (Patch #6.2.1 — confirmed against
            # production files).
            ("purchase_order_id", "tcin", "location_id"),
            ("purchase_order_number", "tcin", "location_id"),
            # Legacy guesses kept as fallbacks for older fixture shapes.
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
        # Tolerates the `_ITEM_DC` (or `_ITEM_<anything>_DC`) granularity token Target
        # adds when the planning grain is distribution center rather than store —
        # the trailing `(?:_[A-Z_]+)?` accepts any extra uppercase/underscore token.
        regex=_pat(r"BI_WEEKLY_PO_PLANNING(?:_[A-Z_]+)?"),
        granularity="item × bi-weekly period (DC- or store-grain)",
        frequency="bi-weekly",
        primary_key_candidates=(
            # If a DC dimension is present, key on it.
            ("tcin", "dc_id", "period_start_date"),
            ("tcin", "dc_number", "period_start_date"),
            ("tcin", "dc_id", "period_end_date"),
            ("tcin", "dc_number", "period_end_date"),
            # Otherwise fall back to item-period key.
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
        primary_key_candidates=_pk_with_loc(
            "fiscal_week_begin_d", "fiscal_week_begin_date",
            "fiscal_week_end_d", "fiscal_week_end_date",
            "week_start_date", "week_end_date", "forecast_week",
        ),
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


@dataclass(frozen=True)
class ParseResult:
    """Result of a successful BPD file parse.

    `method` is one of: 'strict' (polars happy), 'ignore_errors' (polars permissive,
    some rows skipped), 'pandas_permissive' (fell back to pandas+python engine).
    `skipped_rows` is only meaningful when method != 'strict'.
    `primary_error` records the strict-parse failure that triggered fallback (None
    if method == 'strict').
    """

    df: pl.DataFrame
    original_columns: list[str]
    delimiter: str
    method: str
    skipped_rows: int = 0
    primary_error: str | None = None


def _finalize(df: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """Normalize column names and apply type-hint casts. Shared by all attempts."""
    original = list(df.columns)
    df = df.rename({c: _normalize_column_name(c) for c in df.columns})
    df = _cast_known_columns(df)
    return df, original


def _attempt_strict(raw: bytes, delim: str) -> pl.DataFrame:
    """Attempt 1: polars strict. Fail loudly on any schema error.

    `quote_char=None` disables quoted-field parsing entirely (Patch #4, Issue 3).
    Target's tab/pipe-delimited files do not use CSV quoting; field values that
    happen to contain `"` (e.g. `6" Height` in a SKU name) used to be interpreted
    as quoted strings and broke tokenization. With quoting disabled, those
    characters are treated as literal data and the row parses cleanly.

    The two-character literal `""` is Target's NULL placeholder in nullable
    columns (e.g. `purchase_order_active_f`, `parent_tcin`). With quoting
    disabled, polars no longer reduces it to an empty field, so we list it
    explicitly in `null_values` to map it to NULL (Patch #6).
    """
    return pl.read_csv(
        io.BytesIO(raw),
        separator=delim,
        has_header=True,
        infer_schema_length=10000,
        null_values=["", "NULL", "null", '""'],
        try_parse_dates=False,
        truncate_ragged_lines=True,
        ignore_errors=False,
        quote_char=None,
    )


def _attempt_polars_permissive(raw: bytes, delim: str) -> tuple[pl.DataFrame, int]:
    """Attempt 2: polars with `ignore_errors=True`. Skip rows polars can't parse.

    Quoting also disabled (same rationale as `_attempt_strict`).

    Returns (df, skipped_rows). `skipped_rows` is estimated as
    (lines_in_source - header - rows_in_df) and may be 0 if polars accepted
    everything on the retry (e.g. the strict failure was a schema-inference issue
    that ignore_errors smoothed over).
    """
    df = pl.read_csv(
        io.BytesIO(raw),
        separator=delim,
        has_header=True,
        infer_schema_length=10000,
        null_values=["", "NULL", "null", '""'],
        try_parse_dates=False,
        truncate_ragged_lines=True,
        ignore_errors=True,
        quote_char=None,
    )
    total_lines = sum(1 for ln in raw.splitlines() if ln.strip())
    expected_rows = max(0, total_lines - 1)  # subtract header
    skipped = max(0, expected_rows - df.height)
    return df, skipped


def _attempt_pandas_permissive(raw: bytes, delim: str) -> tuple[pl.DataFrame, int]:
    """Attempt 3: pandas python engine with `on_bad_lines='skip'`.

    Slower than polars but tolerates malformed quoting, mixed line endings, BOM,
    embedded delimiters in unquoted fields, etc. We then hand the result back to
    polars (without column casting; `_finalize` will handle that).
    """
    import pandas as pd  # local import — pandas is only needed on this fallback path

    df_pd = pd.read_csv(
        io.BytesIO(raw),
        sep=delim,
        header=0,
        engine="python",
        on_bad_lines="skip",
        encoding_errors="replace",
        dtype=str,  # read everything as string; polars/_cast_known_columns handles types
        keep_default_na=False,
        # `'""'` is Target's NULL placeholder; mirror _attempt_strict (Patch #6).
        na_values=["", "NULL", "null", '""'],
    )
    total_lines = sum(1 for ln in raw.splitlines() if ln.strip())
    expected_rows = max(0, total_lines - 1)
    skipped = max(0, expected_rows - len(df_pd))
    df_pl = pl.from_pandas(df_pd)
    return df_pl, skipped


def read_dataframe(zip_path: Path) -> ParseResult:
    """Parse a BPD zip into a Polars DataFrame with a graceful fallback chain.

    Target ships malformed files occasionally (extra delimiters mid-row, embedded
    quotes that split fields, BOM, mixed line endings). We attempt three parsers
    in order from strictest to most permissive, and report which one succeeded
    via ParseResult.method so the caller can persist that info to the ledger.

    Raises ParseError only if all three attempts fail.
    """
    inner_name, raw = open_zipped_text(zip_path)
    delim = _sniff_delimiter(raw[:64 * 1024])

    # Attempt 1: strict.
    try:
        df = _attempt_strict(raw, delim)
        df, original = _finalize(df)
        return ParseResult(
            df=df, original_columns=original, delimiter=delim, method="strict"
        )
    except Exception as strict_err:
        primary_msg = f"{type(strict_err).__name__}: {strict_err}"
        logger.warning(
            "parse_strict_failed",
            file=zip_path.name,
            inner=inner_name,
            delim=delim,
            error=primary_msg,
        )

    # Attempt 2: polars permissive (ignore_errors).
    try:
        df, skipped = _attempt_polars_permissive(raw, delim)
        df, original = _finalize(df)
        logger.warning(
            "parse_fallback_polars_permissive",
            file=zip_path.name,
            inner=inner_name,
            skipped_rows=skipped,
            primary_error=primary_msg,
        )
        return ParseResult(
            df=df,
            original_columns=original,
            delimiter=delim,
            method="ignore_errors",
            skipped_rows=skipped,
            primary_error=primary_msg,
        )
    except Exception as polars_err:
        polars_msg = f"{type(polars_err).__name__}: {polars_err}"
        logger.warning(
            "parse_fallback_polars_failed",
            file=zip_path.name,
            inner=inner_name,
            error=polars_msg,
        )

    # Attempt 3: pandas python engine with on_bad_lines='skip'.
    try:
        df, skipped = _attempt_pandas_permissive(raw, delim)
        df, original = _finalize(df)
        logger.warning(
            "parse_fallback_pandas_permissive",
            file=zip_path.name,
            inner=inner_name,
            skipped_rows=skipped,
            primary_error=primary_msg,
        )
        return ParseResult(
            df=df,
            original_columns=original,
            delimiter=delim,
            method="pandas_permissive",
            skipped_rows=skipped,
            primary_error=f"{primary_msg} | polars_permissive: {polars_msg}",
        )
    except Exception as pandas_err:
        pandas_msg = f"{type(pandas_err).__name__}: {pandas_err}"

    # All three attempts failed — surface every error.
    sample_lines = raw.splitlines()[:6]
    preview = b"\n".join(sample_lines).decode("utf-8", errors="replace")
    raise ParseError(
        f"{zip_path.name} (inner: {inner_name}): all parse attempts failed.\n"
        f"  strict: {primary_msg}\n"
        f"  polars_permissive: {polars_msg}\n"
        f"  pandas_permissive: {pandas_msg}\n"
        f"delim={delim!r}\nfirst lines:\n{preview}"
    )


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
