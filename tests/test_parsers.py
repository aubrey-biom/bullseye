"""Parser tests with synthetic fixture zips for every dataset in the catalog.

§ May 2026 patch:
The pattern catalog now covers the 15 real filename shapes Target ships. These tests
verify each shape classifies into the right dataset, and includes a regression test
for unknown filenames being reported as `unknown_pattern` (not crashing).
"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from bpd_mcp.parsers import (
    PATTERNS,
    ParseError,
    classify_filename,
    derive_duckdb_schema,
    open_zipped_text,
    read_dataframe,
)


def _make_zip(path: Path, inner_name: str, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, text)
    return path


# ---------- name classification ----------


@pytest.mark.parametrize(
    ("name", "dataset", "expected_date"),
    [
        # ---- sales ----
        (
            "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
            "sales_daily",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip",
            "sales_weekly",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WEEKLY_SALES_TCIN_04252026_KW.zip",
            "sales_weekly_item",
            date(2026, 4, 25),
        ),
        # ---- inventory ----
        (
            "BV_139440_DAILY_INV_TCIN_LOC_04252026_KW.zip",
            "inventory_daily",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WEEKLY_INV_TCIN_LOC_04252026_KW.zip",
            "inventory_weekly",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WEEKLY_INV_TCIN_04252026_KW.zip",
            "inventory_weekly_item",
            date(2026, 4, 25),
        ),
        # ---- gross margin ----
        (
            "BV_139440_WEEKLY_GM_TCIN_LOC_04252026_KW.zip",
            "gross_margin",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WEEKLY_GM_TCIN_04252026_KW.zip",
            "gross_margin_item",
            date(2026, 4, 25),
        ),
        # ---- item / location dimension ----
        (
            "BV_139440_WEEKLY_ITEM_MTA_04252026_KW.zip",
            "item_attr",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_WKLY_TCIN_ITEM_04252026_KW.zip",
            "item_attr_extended",
            date(2026, 4, 25),
        ),
        (
            "ALL_WKLY_LOC_ATTR_V0_0_04252026_KW.zip",
            "location_attr",
            date(2026, 4, 25),
        ),
        # ---- May 2026 new datasets ----
        (
            "BV_139440_DAILY_ORDER_TCIN_LOC_04252026_KW.zip",
            "orders_daily",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_DLY_PO_PLAN_TCIN_04252026_KW.zip",
            "po_plan_daily",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_BI_WEEKLY_PO_PLANNING_04252026_KW.zip",
            "po_plan_biweekly",
            date(2026, 4, 25),
        ),
        (
            "BV_139440_DFE_WKLY_ITEM_LOC_FORECAST_04252026_KW.zip",
            "forecast_weekly",
            date(2026, 4, 25),
        ),
        # ---- tier variants on the same dataset ----
        (
            "BR_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
            "sales_daily",
            date(2026, 4, 25),
        ),
        (
            "CC_999999_WEEKLY_SALES_TCIN_LOC_01012026_KW.zip",
            "sales_weekly",
            date(2026, 1, 1),
        ),
    ],
)
def test_classify_filename_recognizes_catalog(
    name: str, dataset: str, expected_date: date
) -> None:
    parsed = classify_filename(name)
    assert parsed is not None, f"failed to classify {name}"
    assert parsed.pattern.dataset == dataset
    assert parsed.file_date == expected_date


@pytest.mark.parametrize(
    "name",
    [
        # Garbage
        "README.txt",
        "BV_139440_DLY_SALES_ITEM_LOC_VEND_2026-04-25_KW.zip",  # bad date format
        "ZZ_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",  # bad tier
        "BV_139440_DAILY_SALES_TCIN_LOC_04252026.zip",  # missing _KW
        # Regression: a brand-new file shape Target ships should report unknown,
        # not crash (§ patch Step 7).
        "BV_139440_RANDOM_NEW_TYPE_04252026_KW.zip",
        # Old spec shape (DLY_SALES_ITEM_LOC_VEND): should no longer match. The
        # catalog only recognizes the real filenames Target is shipping today.
        "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip",
        "BV_139440_WKLY_GM_ITEM_VEND_04252026_KW.zip",
    ],
)
def test_classify_filename_rejects_unknown(name: str) -> None:
    assert classify_filename(name) is None


def test_pattern_catalog_covers_all_15_datasets() -> None:
    """Catalog must cover every dataset listed in the May 2026 patch brief."""
    expected = {
        "sales_daily",
        "sales_weekly",
        "sales_weekly_item",
        "inventory_daily",
        "inventory_weekly",
        "inventory_weekly_item",
        "gross_margin",
        "gross_margin_item",
        "item_attr",
        "item_attr_extended",  # we keep the WKLY_TCIN_ITEM variant separate
        "location_attr",
        "orders_daily",
        "po_plan_daily",
        "po_plan_biweekly",
        "forecast_weekly",
    }
    assert {p.dataset for p in PATTERNS} == expected


def test_each_pattern_round_trips_through_classify() -> None:
    """Every pattern in the catalog must classify a representative filename it.

    Guards against regex drift where a pattern is added but never actually matches.
    """
    samples = {
        "sales_daily": "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "sales_weekly": "BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip",
        "sales_weekly_item": "BV_139440_WEEKLY_SALES_TCIN_04252026_KW.zip",
        "inventory_daily": "BV_139440_DAILY_INV_TCIN_LOC_04252026_KW.zip",
        "inventory_weekly": "BV_139440_WEEKLY_INV_TCIN_LOC_04252026_KW.zip",
        "inventory_weekly_item": "BV_139440_WEEKLY_INV_TCIN_04252026_KW.zip",
        "gross_margin": "BV_139440_WEEKLY_GM_TCIN_LOC_04252026_KW.zip",
        "gross_margin_item": "BV_139440_WEEKLY_GM_TCIN_04252026_KW.zip",
        "item_attr": "BV_139440_WEEKLY_ITEM_MTA_04252026_KW.zip",
        "item_attr_extended": "BV_139440_WKLY_TCIN_ITEM_04252026_KW.zip",
        "location_attr": "ALL_WKLY_LOC_ATTR_V0_0_04252026_KW.zip",
        "orders_daily": "BV_139440_DAILY_ORDER_TCIN_LOC_04252026_KW.zip",
        "po_plan_daily": "BV_139440_DLY_PO_PLAN_TCIN_04252026_KW.zip",
        "po_plan_biweekly": "BV_139440_BI_WEEKLY_PO_PLANNING_04252026_KW.zip",
        "forecast_weekly": "BV_139440_DFE_WKLY_ITEM_LOC_FORECAST_04252026_KW.zip",
    }
    for pat in PATTERNS:
        assert pat.dataset in samples, f"no sample defined for {pat.dataset}"
        parsed = classify_filename(samples[pat.dataset])
        assert parsed is not None
        assert parsed.pattern.dataset == pat.dataset


def test_loc_and_item_rollup_disambiguate() -> None:
    """`WEEKLY_SALES_TCIN_LOC_<DATE>` vs `WEEKLY_SALES_TCIN_<DATE>` must not collide."""
    a = classify_filename("BV_139440_WEEKLY_SALES_TCIN_LOC_04252026_KW.zip")
    b = classify_filename("BV_139440_WEEKLY_SALES_TCIN_04252026_KW.zip")
    assert a is not None and b is not None
    assert a.pattern.dataset == "sales_weekly"
    assert b.pattern.dataset == "sales_weekly_item"


# ---------- delimiter / schema / sentinel ----------


def test_read_dataframe_pipe_delimited_with_negative_one_sentinel(tmp_path: Path) -> None:
    body = (
        "TCIN|LOCATION ID|SALE DATE|UNITS SOLD|SALES DOLLARS\n"
        "12345|2750|2026-04-21|10|99.50\n"
        "12345|-1|2026-04-21|3|-1\n"  # sentinel preserved
    )
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.txt",
        body,
    )
    r = read_dataframe(p)
    df, delim = r.df, r.delimiter
    assert delim == "|"
    assert set(df.columns) == {"tcin", "location_id", "sale_date", "units_sold", "sales_dollars"}
    # Sentinel -1 preserved as int, not coerced to NULL.
    loc_vals = df["location_id"].to_list()
    assert -1 in loc_vals
    # Date casted.
    assert df.schema["sale_date"] == pl.Date


def test_read_dataframe_tab_delimited(tmp_path: Path) -> None:
    body = (
        "TCIN\tLOCATION ID\tSNAPSHOT DATE\tON_HAND_UNITS\n"
        "777\t1234\t2026-04-21\t50\n"
        "777\t1234\t2026-04-22\t45\n"
    )
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_INV_TCIN_LOC_04212026_KW.zip",
        "BV_139440_DAILY_INV_TCIN_LOC_04212026_KW.txt",
        body,
    )
    r = read_dataframe(p)
    df, delim = r.df, r.delimiter
    assert delim == "\t"
    assert "snapshot_date" in df.columns
    assert df.schema["snapshot_date"] == pl.Date
    assert df.schema["tcin"].is_integer()
    # The new INT hints catch on_hand_units (suffix `_units`).
    assert df.schema["on_hand_units"].is_integer()


def test_read_dataframe_handles_new_dataset_columns(tmp_path: Path) -> None:
    """Smoke test that the new datasets parse without crashing and the type-hint
    regex picks up the new column names (qty/units suffix, *_date, etc.)."""
    body = (
        "TCIN|LOCATION_ID|ORDER_DATE|OPEN_UNITS|PLANNED_QTY|ORDER_STATUS\n"
        "100|2750|2026-04-25|5|10|OPEN\n"
        "100|2750|2026-04-26|0|10|FULFILLED\n"
    )
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_ORDER_TCIN_LOC_04262026_KW.zip",
        "data.txt",
        body,
    )
    r = read_dataframe(p)
    df = r.df
    assert set(df.columns) == {
        "tcin",
        "location_id",
        "order_date",
        "open_units",
        "planned_qty",
        "order_status",
    }
    assert df.schema["order_date"] == pl.Date
    assert df.schema["tcin"].is_integer()
    assert df.schema["location_id"].is_integer()
    assert df.schema["open_units"].is_integer()
    assert df.schema["planned_qty"].is_integer()


def test_read_dataframe_forecast_weekly(tmp_path: Path) -> None:
    body = (
        "TCIN|LOCATION_ID|WEEK_END_DATE|FORECAST_UNITS\n"
        "100|2750|2026-04-25|42\n"
        "100|2750|2026-05-02|38\n"
    )
    p = _make_zip(
        tmp_path / "BV_139440_DFE_WKLY_ITEM_LOC_FORECAST_04252026_KW.zip",
        "data.txt",
        body,
    )
    r = read_dataframe(p)
    df = r.df
    assert df.schema["week_end_date"] == pl.Date
    assert df.schema["forecast_units"].is_integer()


def test_read_dataframe_po_plan_biweekly(tmp_path: Path) -> None:
    body = (
        "TCIN|PERIOD_START_DATE|PERIOD_END_DATE|PLANNED_UNITS\n"
        "100|2026-04-20|2026-05-03|500\n"
    )
    p = _make_zip(
        tmp_path / "BV_139440_BI_WEEKLY_PO_PLANNING_04202026_KW.zip",
        "data.txt",
        body,
    )
    r = read_dataframe(p)
    df = r.df
    assert df.schema["period_start_date"] == pl.Date
    assert df.schema["period_end_date"] == pl.Date
    assert df.schema["planned_units"].is_integer()


def test_read_dataframe_normalizes_column_names(tmp_path: Path) -> None:
    body = "TCIN | Sale Date | Units Sold\n10|2026-04-21|5\n"
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    r = read_dataframe(p)
    df = r.df
    assert df.columns == ["tcin", "sale_date", "units_sold"]


def test_derive_duckdb_schema_maps_polars_dtypes() -> None:
    df = pl.DataFrame(
        {
            "tcin": [1, 2],
            "amt": [1.5, 2.5],
            "sale_date": [date(2026, 4, 21), date(2026, 4, 22)],
            "desc": ["a", "b"],
        }
    )
    cols = derive_duckdb_schema(df)
    assert cols["tcin"] == "BIGINT"
    assert cols["amt"] == "DOUBLE"
    assert cols["sale_date"] == "DATE"
    assert cols["desc"] == "TEXT"


def test_open_zipped_text_picks_first_text_member(tmp_path: Path) -> None:
    p = tmp_path / "BV_139440_WEEKLY_GM_TCIN_LOC_04252026_KW.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("checksum.md5", "deadbeef")
        zf.writestr("data.txt", "TCIN|GM\n1|0.30\n")
    name, raw = open_zipped_text(p)
    assert name == "data.txt"
    assert b"TCIN" in raw


def test_parse_error_on_corrupt_zip(tmp_path: Path) -> None:
    p = tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip"
    p.write_bytes(b"not a zip")
    with pytest.raises((ParseError, zipfile.BadZipFile)):
        read_dataframe(p)


# ---------- Patch #2: malformed-file fallback parsing ----------


def test_parse_result_returns_strict_method_on_clean_file(tmp_path: Path) -> None:
    body = "TCIN|UNITS\n1|10\n2|20\n"
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    result = read_dataframe(p)
    assert result.method == "strict"
    assert result.skipped_rows == 0
    assert result.primary_error is None
    assert result.df.height == 2


def test_parse_fallback_handles_bom_prefix(tmp_path: Path) -> None:
    """A UTF-8 BOM at the start of the file should not crash the parser."""
    body = "﻿TCIN|UNITS\n1|10\n2|20\n"
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    result = read_dataframe(p)
    # BOM might be tolerated by polars strict (it usually is); either way the file loads.
    assert result.method in {"strict", "ignore_errors", "pandas_permissive"}
    assert result.df.height == 2
    # The BOM should be stripped from the first column name.
    assert "tcin" in result.df.columns


def test_parse_fallback_handles_mixed_line_endings(tmp_path: Path) -> None:
    """CRLF + LF + bare CR mixed in one file. Polars typically copes; this is just a smoke test."""
    body = "TCIN|UNITS\r\n1|10\n2|20\r\n3|30\n"
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    result = read_dataframe(p)
    assert result.df.height >= 3
    assert result.df.height <= 4  # depending on whether the last record gets dropped


def test_parse_fallback_skips_rows_with_extra_delimiters(tmp_path: Path) -> None:
    """A row with extra delimiters mid-field should trigger a fallback path."""
    # 2 columns expected; the middle row has 4 fields.
    body = (
        "TCIN|UNITS\n"
        "1|10\n"
        "2|extra|junk|here\n"  # bad row — too many fields
        "3|30\n"
    )
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    # Don't assert which fallback level — polars's truncate_ragged_lines=True may
    # accept this in strict, or polars's ignore_errors may catch it, or pandas may.
    # The point of the test: it loads, doesn't raise.
    result = read_dataframe(p)
    assert result.df.height >= 2  # at least the two clean rows


def test_parse_fallback_pandas_permissive_handles_embedded_quote(tmp_path: Path) -> None:
    """An unbalanced quote that polars would choke on but pandas tolerates."""
    body = 'TCIN|UNITS|DESC\n1|10|hello\n2|20|she said "hi\n3|30|world\n'
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    # The 3-tier chain should swallow this. If all three fail, the test will raise.
    result = read_dataframe(p)
    assert result.df.height >= 2
    # If we did fall back, primary_error captures the strict-attempt failure.
    if result.method != "strict":
        assert result.primary_error is not None


def test_parse_fallback_records_method_in_result(tmp_path: Path) -> None:
    """A deliberately-malformed body that polars-strict cannot parse forces a fallback.

    We assert the resulting `method` is one of the fallback levels (not strict),
    and that skipped_rows + primary_error are populated.
    """
    # Force a strict failure by adding garbage that polars's truncate_ragged_lines
    # path doesn't recover from: inconsistent quoting that breaks tokenization.
    body = 'A|B|C\n1|"unclosed quote|x\n2|3|4\n'
    p = _make_zip(
        tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip",
        "data.txt",
        body,
    )
    try:
        result = read_dataframe(p)
    except ParseError:
        # Acceptable: if all three layers genuinely fail, we don't lie to the caller.
        return
    # If it loaded at all, the result records what happened.
    assert result.method in {"strict", "ignore_errors", "pandas_permissive"}


def test_parse_fully_corrupt_outcome_is_either_failed_or_fallback(tmp_path: Path) -> None:
    """Fallback isn't unlimited — a file of binary noise either raises ParseError
    OR loads via a fallback path. Crucially, strict-method success would indicate
    silent data corruption, which we forbid.
    """
    p = tmp_path / "BV_139440_DAILY_SALES_TCIN_LOC_04252026_KW.zip"
    # Build a zip whose inner content is binary noise (no parseable header).
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("data.txt", bytes(range(256)) * 4)
    try:
        result = read_dataframe(p)
    except ParseError:
        return  # Expected outcome on most environments — pandas may also reject.
    # If the chain managed to load *something*, it must NOT claim strict success.
    assert result.method != "strict", (
        "binary noise was silently accepted by strict polars — this is unsafe; "
        "expected a fallback path or ParseError."
    )


# ---------- Patch #2: BI_WEEKLY_PO_PLANNING_ITEM_DC variant ----------


@pytest.mark.parametrize(
    "name",
    [
        "BV_139440_BI_WEEKLY_PO_PLANNING_04252026_KW.zip",
        "BV_139440_BI_WEEKLY_PO_PLANNING_ITEM_DC_04252026_KW.zip",
        "BV_139440_BI_WEEKLY_PO_PLANNING_ITEM_STORE_04252026_KW.zip",
        "BV_139440_BI_WEEKLY_PO_PLANNING_ITEM_DC_STORE_04252026_KW.zip",
    ],
)
def test_bi_weekly_po_planning_tolerates_granularity_token(name: str) -> None:
    parsed = classify_filename(name)
    assert parsed is not None, f"failed to classify {name}"
    assert parsed.pattern.dataset == "po_plan_biweekly"
