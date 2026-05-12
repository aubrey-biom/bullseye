"""Static drift guards identified by the Patch #3 codebase audit.

These tests pin parallel sources of truth together so a future code change can't
silently let them drift apart. They're cheap regressions for the kinds of bugs we
keep being bitten by.
"""

from __future__ import annotations

import typing

from bpd_mcp import parsers, schemas


def _literal_values(t) -> set[str]:
    """Return the set of string values in a `Literal[...]` type alias."""
    return set(typing.get_args(t))


def test_known_dataset_enum_matches_pattern_catalog() -> None:
    """`schemas.KnownDataset` must match the datasets defined in `parsers.PATTERNS`.

    If you add a pattern to parsers.PATTERNS without updating the Literal in
    schemas.py, MCP arg validation breaks for the new dataset name. This guard
    fires immediately at test time so the omission is caught at code review,
    not at runtime after a real sync.
    """
    pattern_datasets = {p.dataset for p in parsers.PATTERNS}
    enum_datasets = _literal_values(schemas.KnownDataset)
    assert pattern_datasets == enum_datasets, (
        f"drift detected:\n"
        f"  in patterns only: {pattern_datasets - enum_datasets}\n"
        f"  in KnownDataset only: {enum_datasets - pattern_datasets}"
    )


def test_parsers_dataset_literal_matches_pattern_catalog() -> None:
    """Internal `parsers.Dataset` Literal must also match PATTERNS."""
    pattern_datasets = {p.dataset for p in parsers.PATTERNS}
    literal_datasets = _literal_values(parsers.Dataset)
    assert pattern_datasets == literal_datasets, (
        f"drift detected:\n"
        f"  in patterns only: {pattern_datasets - literal_datasets}\n"
        f"  in Dataset only: {literal_datasets - pattern_datasets}"
    )


def test_expected_ledger_columns_matches_warehouse_ddl() -> None:
    """The health check's EXPECTED_LEDGER_COLUMNS must match the warehouse DDL.

    Otherwise the warehouse_schema_current check would report drift against
    its own internal expectation — meaningless noise.
    """
    import tempfile
    from pathlib import Path

    from bpd_mcp.tools.admin import EXPECTED_LEDGER_COLUMNS
    from bpd_mcp.warehouse import Warehouse

    with tempfile.TemporaryDirectory() as td:
        wh = Warehouse(Path(td) / "bpd.duckdb")
        try:
            _, rows = wh.execute_sql("PRAGMA table_info('_file_ledger')")
            actual = {r[1] for r in rows}
        finally:
            wh.close()
    assert set(EXPECTED_LEDGER_COLUMNS) == actual, (
        f"drift between EXPECTED_LEDGER_COLUMNS and METADATA_DDL+_MIGRATIONS:\n"
        f"  expected but missing in DDL: {set(EXPECTED_LEDGER_COLUMNS) - actual}\n"
        f"  in DDL but not expected:    {actual - set(EXPECTED_LEDGER_COLUMNS)}"
    )


def test_expected_tool_count_matches_registered_tools() -> None:
    """The health check's EXPECTED_TOOL_COUNT must equal the number of registered tools.

    Otherwise mcp_self_check immediately warns or fails on a fresh install.
    """
    from bpd_mcp.server import mcp
    from bpd_mcp.tools.admin import EXPECTED_TOOL_COUNT

    n = len(mcp._tool_manager._tools)
    assert n == EXPECTED_TOOL_COUNT, (
        f"tool count drift: server has {n} tools, health check expects "
        f"{EXPECTED_TOOL_COUNT}. Either add a tool or bump EXPECTED_TOOL_COUNT "
        f"in tools/admin.py."
    )
