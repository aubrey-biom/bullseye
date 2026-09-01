"""Tool implementations registered on the FastMCP server.

Two modules remain:

* `query`  — the analytics surface (run_sql, describe_schema, sales/inventory/
  sell-through/orders/PO-plan/forecast).
* `admin`  — list_datasets, bigquery_status, data_freshness, health_check.

`files` and `sync` are gone with the Kiteworks ingest half. Nothing in this
package downloads, parses or writes data any more; the whole server is a
read-only BigQuery analytics layer.
"""
