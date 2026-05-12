"""Parser tests with synthetic fixture zips for every dataset in the catalog."""

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
        ("BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip", "sales_daily", date(2026, 4, 25)),
        ("BV_139440_DLY_INV_ITEM_LOC_VEND_04252026_KW.zip", "inventory_daily", date(2026, 4, 25)),
        ("BV_139440_WKLY_SALES_ITEM_LOC_VEND_04252026_KW.zip", "sales_weekly", date(2026, 4, 25)),
        ("BV_139440_WKLY_INV_ITEM_LOC_VEND_04252026_KW.zip", "inventory_weekly", date(2026, 4, 25)),
        ("BV_139440_WKLY_ITM_VEND_ATTR_04252026_KW.zip", "item_attr", date(2026, 4, 25)),
        ("BV_139440_WKLY_GM_ITEM_VEND_04252026_KW.zip", "gross_margin", date(2026, 4, 25)),
        ("ALL_WKLY_LOC_ATTR_V0_0_04252026_KW.zip", "location_attr", date(2026, 4, 25)),
        ("BR_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip", "sales_daily", date(2026, 4, 25)),
        ("CC_999999_WKLY_SALES_ITEM_LOC_VEND_01012026_KW.zip", "sales_weekly", date(2026, 1, 1)),
    ],
)
def test_classify_filename_recognizes_catalog(name: str, dataset: str, expected_date: date) -> None:
    parsed = classify_filename(name)
    assert parsed is not None, f"failed to classify {name}"
    assert parsed.pattern.dataset == dataset
    assert parsed.file_date == expected_date


@pytest.mark.parametrize(
    "name",
    [
        "README.txt",
        "BV_139440_DLY_SALES_ITEM_LOC_VEND_2026-04-25_KW.zip",  # bad date format
        "ZZ_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip",  # bad tier
        "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026.zip",  # missing _KW
    ],
)
def test_classify_filename_rejects_unknown(name: str) -> None:
    assert classify_filename(name) is None


def test_pattern_catalog_covers_all_seven_datasets() -> None:
    expected = {
        "sales_daily",
        "inventory_daily",
        "sales_weekly",
        "inventory_weekly",
        "item_attr",
        "location_attr",
        "gross_margin",
    }
    assert {p.dataset for p in PATTERNS} == expected


# ---------- delimiter / schema / sentinel ----------


def test_read_dataframe_pipe_delimited_with_negative_one_sentinel(tmp_path: Path) -> None:
    body = (
        "TCIN|LOCATION ID|SALE DATE|UNITS SOLD|SALES DOLLARS\n"
        "12345|2750|2026-04-21|10|99.50\n"
        "12345|-1|2026-04-21|3|-1\n"  # sentinel preserved
    )
    p = _make_zip(
        tmp_path / "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip",
        "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.txt",
        body,
    )
    df, _original_cols, delim = read_dataframe(p)
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
        tmp_path / "BV_139440_DLY_INV_ITEM_LOC_VEND_04212026_KW.zip",
        "BV_139440_DLY_INV_ITEM_LOC_VEND_04212026_KW.txt",
        body,
    )
    df, _orig, delim = read_dataframe(p)
    assert delim == "\t"
    assert "snapshot_date" in df.columns
    assert df.schema["snapshot_date"] == pl.Date
    # tcin should be int.
    assert df.schema["tcin"].is_integer()


def test_read_dataframe_normalizes_column_names(tmp_path: Path) -> None:
    body = "TCIN | Sale Date | Units Sold\n10|2026-04-21|5\n"
    p = _make_zip(
        tmp_path / "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip",
        "data.txt",
        body,
    )
    df, _, _ = read_dataframe(p)
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
    p = tmp_path / "BV_139440_WKLY_GM_ITEM_VEND_04252026_KW.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("checksum.md5", "deadbeef")
        zf.writestr("data.txt", "TCIN|GM\n1|0.30\n")
    name, raw = open_zipped_text(p)
    assert name == "data.txt"
    assert b"TCIN" in raw


def test_parse_error_on_corrupt_zip(tmp_path: Path) -> None:
    p = tmp_path / "BV_139440_DLY_SALES_ITEM_LOC_VEND_04252026_KW.zip"
    p.write_bytes(b"not a zip")
    with pytest.raises((ParseError, zipfile.BadZipFile)):
        read_dataframe(p)
