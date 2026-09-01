"""Compatibility re-export for the data layer, which now lives in `bq.py`.

The DuckDB `Warehouse` and its `ReadOnlyView` facade are gone. `bq.py` holds
the BigQuery replacement: the logical-table registry, CTE injection, and
`BigQueryWarehouse`.

This module survives as a thin shim ONLY so that existing imports and type
annotations keep resolving:

    from ..warehouse import Warehouse, quote_ident      # tools/query.py
    def get_sales_summary(warehouse: Warehouse, ...)    # every analytics tool

Keeping the module path stable keeps those diffs limited to actual behaviour
changes. New code should import from `.bq` directly.

Deliberately NOT re-exported, because they no longer exist and a shim for them
would be a lie: `ReadOnlyView` (read-only is now a property of the credential —
the service account holds dataViewer + jobUser and gets a 403 on
`bigquery.tables.create`, which is strictly stronger than a transaction
wrapper), `ensure_views`, `backup_to`, `cleanup_legacy_snapshot`,
`upsert_dataframe`, and every ledger method.
"""

from __future__ import annotations

from .bq import BigQueryWarehouse, Warehouse, quote_ident

__all__ = ["BigQueryWarehouse", "Warehouse", "quote_ident"]
