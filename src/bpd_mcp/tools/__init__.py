"""Tool implementations registered on the FastMCP server.

Three modules:

* `query`  — the Target analytics surface (run_sql, describe_schema, sales/
  inventory/sell-through/orders/PO-plan/forecast).
* `dtc`    — the DTC (Shopify) and paid-media analytics added in Phase 2 of
  the DTC performance / pacing work (dtc_sales_summary, ads_performance,
  marketing_efficiency).
* `admin`  — list_datasets, bigquery_status, data_freshness, health_check.

`files` and `sync` are gone with the Kiteworks ingest half. Nothing in this
package downloads, parses or writes data any more; the whole server is a
read-only BigQuery analytics layer.
"""
