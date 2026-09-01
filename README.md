# bpd-mcp — Target BPD MCP Server

A stdio MCP server that lets Claude analyze Target's **Business Partner Data**
(daily/weekly sales, inventory, orders, PO plans, DFE forecast, item and
location attributes, gross margin) out of **BigQuery**.

The server is a **read-only analytics layer**. It does not download, parse or
store anything. An independent Kiteworks → GCS → BigQuery pipeline lands the BPD
file set into `biom-reporting-s26` each morning; this server reads what that
pipeline produced. As of 2026-09-01 `bpd_meta.ingestion_state` holds **834 files
across all 18 patterns**, most recently downloaded at **06:49 UTC** that day.

Python 3.11+. Framework: **FastMCP**. Data layer: `google-cloud-bigquery`
against a service account holding **`dataViewer` + `jobUser`** — it can read
everything and write nothing.

## Why it is not DuckDB any more

The previous version maintained a local DuckDB warehouse at
`~/.bpd-mcp/bpd.duckdb`. DuckDB permits **exactly one process** to hold a
database file: while a read-write connection is open, a second process cannot
open the file at all — not even with `read_only=True`. Claude Desktop now
spawns a **second copy of this server** for Cowork/Code sessions, and that copy
crashed on the lock. That was the reported symptom: *the server does not start.*

BigQuery is a network service, so N server processes hold N independent HTTPS
clients and never contend. Removing the lock **is** the point of the change. As
a direct consequence, startup takes no file lock, writes no state file, and
performs no snapshot cleanup — see the module docstring of `server.py`, which
enumerates each single-process assumption that was removed and why.

---

## Quickstart

```bash
# 0. Install
pip install uv               # if you don't have it
uv sync                      # creates .venv and installs everything

# 1. Point at BigQuery. Either works:
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/claude-code-bq-readonly.json
#   ...or, for environments that can only pass strings:
export GCP_SA_KEY_B64="$(base64 -w0 /path/to/claude-code-bq-readonly.json)"

# 2. Verify the install is healthy (one command; no MCP client needed).
./scripts/verify_install.sh

# 3. Run the MCP server (stdio transport — for Claude Desktop / Claude Code)
uv run bpd-mcp
```

There is no auth bootstrap step and no first sync. The data is already there —
ask Claude to "describe the schema" and start querying.

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bpd": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/bpd-mcp", "run", "bpd-mcp"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/absolute/path/to/claude-code-bq-readonly.json",
        "BPD_BQ_PROJECT": "biom-reporting-s26",
        "BPD_BQ_LOCATION": "us-central1",
        "BPD_VENDOR_ID": "139440",
        "BPD_VENDOR_TIER": "BV"
      }
    }
  }
}
```

If your launcher can only pass strings, swap `GOOGLE_APPLICATION_CREDENTIALS`
for `"GCP_SA_KEY_B64": "<base64 of the service-account JSON>"`. The server
materializes it to `~/.config/gcloud/biom-bq-sa.json` at mode `0600` and never
logs or returns the key bytes.

**The same config may be used by more than one Claude surface at the same
time.** Running Claude Desktop and Claude Code against this server
concurrently is supported and is the reason the data layer changed.

---

## Configuration

Read from environment variables and (optionally) a `.env` file at the project
root. See `.env.example`.

| Var                         | Default              | Notes                                                                                     |
| --------------------------- | -------------------- | ----------------------------------------------------------------------------------------- |
| `GOOGLE_APPLICATION_CREDENTIALS` | —               | Path to the service-account JSON. Read straight from `os.environ`, never a settings field. |
| `GCP_SA_KEY_B64`            | —                    | Base64 of that JSON, for launchers that only pass strings. Materialized at `0600`.        |
| `BPD_BQ_PROJECT`            | `biom-reporting-s26` | GCP project.                                                                              |
| `BPD_BQ_LOCATION`           | `us-central1`        | **Required.** An empty location makes `INFORMATION_SCHEMA` silently return zero rows instead of erroring. Validated non-empty. |
| `BPD_BQ_MAX_BYTES_BILLED`   | 20 GiB               | Hard `maximum_bytes_billed` on every job.                                                 |
| `BPD_BQ_WARN_BYTES`         | 1 GiB                | The pre-flight dry-run gate logs a warning above this.                                    |
| `BPD_BQ_DATERANGE_TTL_S`    | `900`                | TTL for the combined date-range sweep (~527 MB per refresh — the one metadata query that costs money). |
| `BPD_BQ_ROWCOUNT_TTL_S`     | `300`                | TTL for `__TABLES__` row counts (0 bytes).                                                |
| `BPD_EXPORT_MAX_ROWS`       | `200000`             | Cap for `bpd_export_query_to_csv`. Lowered from 1,000,000: on per-byte billing an unguarded export is a money question, not a disk question. |
| `BPD_VENDOR_ID`             | `139440`             | Biom's BPID. Identity only.                                                               |
| `BPD_VENDOR_TIER`           | `BV`                 | `BV` Basic, `BR` Brand, `CC` Category Captain.                                            |
| `BPD_DATA_DIR`              | `~/.bpd-mcp`         | Root for **outputs only**.                                                                |
| `BPD_LOG_LEVEL`             | `INFO`               |                                                                                           |

The data dir now holds outputs, not data:

```
~/.bpd-mcp/
├── exports/              # bpd_export_query_to_csv writes here
└── logs/bpd-mcp.log      # rotating JSON log (10 MB × 5)
```

`raw/`, `extracted/`, `backups/`, `bpd.duckdb`, `bpd.duckdb.ro` and
`tokens.json` are all gone. Nothing reads them. Deleting them by hand is safe
once every old server process has exited.

---

## Data model: logical tables

The analytics tools reference **15 logical tables** by bare name. They are not
BigQuery views — the service account cannot create views — so the server
injects each referenced one as a CTE immediately before the query runs. From a
caller's point of view (including `bpd_run_sql`) they behave exactly like
tables:

```sql
SELECT tcin, SUM(sale_quantity) AS units
FROM sales_daily
WHERE sales_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY)
GROUP BY tcin ORDER BY units DESC
```

| Logical table           | BigQuery source                                             | Note |
| ----------------------- | ----------------------------------------------------------- | ---- |
| `sales_daily`           | `biom_canvas.fct_target_sales` (`data_grain='daily'`)        | |
| `sales_weekly`          | `fct_target_sales` weekly/history ∪ `bpd_raw.weekly_sales_tcin_loc` | union covers the canvas weekly gap; boundary is computed, so it self-heals |
| `sales_weekly_item`     | `bpd_raw.weekly_sales_tcin`                                  | item-grain rollup, feed stopped 2026-05-16 |
| `inventory_daily`       | `biom_canvas.fct_target_inventory` (`daily`)                 | `inventory_date` aliased to `business_d` |
| `inventory_weekly`      | `fct_target_inventory` (`history_weekly`) ∪ `bpd_raw.weekly_inv_tcin_loc` | |
| `inventory_weekly_item` | `bpd_raw.weekly_inv_tcin`                                    | feed stopped 2026-05-16 |
| `gross_margin`          | `biom_canvas.fct_target_gross_margin` ∪ `bpd_raw.history_gm_weekly` | `fiscal_week_end_date` aliased to `fiscal_week_end_d` |
| `gross_margin_item`     | `bpd_raw.weekly_gm_tcin`                                     | feed stopped 2026-05-16 |
| `orders_daily`          | `bpd_raw.daily_order_tcin_loc`                               | **de-duplicated** to the newest `snapshot_d` per PO line |
| `po_plan_daily`         | `bpd_raw.dly_po_plan_tcin`                                   | accumulating snapshot, **not** de-duplicated here |
| `po_plan_biweekly`      | `bpd_raw.bi_weekly_po_planning_item_dc`                      | accumulating snapshot, **not** de-duplicated here |
| `forecast_weekly`       | `bpd_raw.dfe_wkly_item_loc_forecast`                         | **de-duplicated** to the newest `last_update_d` per (tcin, location, week) |
| `item_attr`             | `bpd_raw.weekly_item_mta`                                    | EAV form |
| `item_attr_extended`    | `bpd_raw.wkly_tcin_item`                                     | |
| `location_attr`         | `bpd_raw.wkly_loc_attr_v0_0`                                 | |

Two of these carry a **latest-state reduction** that is the difference between
right and catastrophically wrong: `orders_daily` unreduced reports 14.2 M open
units instead of ~0.5 M (28×), and `forecast_weekly` unreduced reports 6.9 M
forecast units instead of ~1.06 M (6.5×). `bpd_describe_schema` surfaces the
reduction per table as `latest_state_note`. The two `po_plan_*` tables are
accumulating snapshots **by design** and are reduced by `bpd_get_upcoming_pos`
instead, which filters each source to its own `MAX(business_d)` — never add a
second reduction in the registry, the two would fight silently.

`row_count` in `bpd_describe_schema` / `bpd_list_datasets` is the **base
table's** count from `__TABLES__` (0 bytes), so it overstates every filtered or
de-duplicated table. Counting through the CTEs instead would cost ~333 MB per
call. `row_count_basis: "base_table"` marks it.

### Adding a table (the extension seam)

Adding a source — Shopify, Loop, anything non-BPD — is a **config change**:
append one `LogicalTable` to `bq.LOGICAL_TABLES`, then add the matching
`COLUMN_ROLES`, `DATASET_KINDS` and `FEED_KINDS` entries and extend
`schemas.KnownDataset`. CTE injection, `describe()`, `table_exists`,
`resolve_column`, `detect_date_column`, `bpd_list_datasets`, the health checks
and the drift guards all read that one dict. The full checklist is a comment at
the top of `src/bpd_mcp/bq.py`.

---

## Tool reference

14 tools, all prefixed `bpd_`. Every tool accepts `response_format` of
`markdown` (default) or `json`.

### Catalog & query

| Tool                         | Purpose |
| ---------------------------- | ------- |
| `bpd_list_datasets`          | Per logical table: row count, `feed_kind`, `status` (active/retired), snapshot range (`min/max_date` = freshness) AND content range (`content_max_date` = how far `order_d` / fiscal weeks / ETAs reach), plus how many source files the pipeline has landed and when. |
| `bpd_describe_schema`        | Every logical table, its columns and types, the base table behind it, and any latest-state reduction. Also the MCP resource `bpd://schema`. |
| `bpd_run_sql`                | Arbitrary BigQuery Standard SQL. Reference logical tables by bare name. Read-only at the credential layer AND at the validator. Dry-run first for cost, then wrapped in `LIMIT N`; `extra.estimated_bytes_scanned` is echoed on every response. |
| `bpd_export_query_to_csv`    | Same query path, written to `~/.bpd-mcp/exports/<filename>`. Row cap from `BPD_EXPORT_MAX_ROWS`. |

### Analytics

| Tool                         | Purpose |
| ---------------------------- | ------- |
| `bpd_get_sales_summary`      | Units (and dollars when available) by `day`/`week`/`month` with optional filters. Echoes the effective date range, flags partial boundary buckets, and reports the other sales table's coverage in `extra.alternative_source`. |
| `bpd_get_top_skus`           | Top-N SKUs by units or dollars over a date range. A no-arg call spans all history; the title and `extra` say exactly what period that is. |
| `bpd_get_inventory_snapshot` | Latest known on-hand per TCIN × location at or before a date. `extra.staleness` counts pairs carried forward across feed gaps; `max_staleness_days` excludes them. |
| `bpd_get_sell_through`       | Sales + latest inventory → weeks-of-supply and sell-through. `max_staleness_days` drops stale inventory pairs — WOS from 10-week-old on-hand is misleading. |
| `bpd_get_open_orders`        | Outstanding Target POs summed by SKU. Open units are **derived** as `revised_order_q − item_received_q − cancel_remaining_order_q`, keeping lines > 0. `as_of_date` filters by PO **creation** date, not time travel. |
| `bpd_get_upcoming_pos`       | `po_plan_daily` + `po_plan_biweekly`, each filtered to its **latest `business_d` snapshot**. Windows on `order_d`, grouped by (tcin, week, **source**) so the two plans never blend. |
| `bpd_get_forecast_vs_actual` | DFE `forecast_weekly` vs `sales_weekly` on a coverage-honest (tcin, location, week) spine — only **matched** cells produce variance; unmatched volume is counted in `extra.coverage`, never zero-filled. `variance_pct` is a true percent. `snapshot_policy`: `latest_available` (default) or `pre_week`. |

### Admin

| Tool                    | Purpose |
| ----------------------- | ------- |
| `bpd_bigquery_status`   | Which identity we query as (`SESSION_USER()`), where the credential came from (a path or env-var name — never key bytes), project, location, reachable datasets, and an explicit `write_capability: none`. **Replaces `bpd_auth_status`.** |
| `bpd_data_freshness`    | Per-dataset snapshot and content date ranges, plus the upstream pipeline's own per-pattern ledger: file counts, newest file date, last download, lag in days. **Replaces `bpd_cache_status`.** |
| `bpd_health_check`      | 12-check audit (see below). First call when diagnosing anything. |

**Removed in this version**, with no replacement: `bpd_list_top_folders`,
`bpd_list_folder_contents`, `bpd_get_file_metadata`, `bpd_search_files`,
`bpd_sync_new_files`, `bpd_refresh_dataset`, `bpd_reingest_local`,
`bpd_clear_cache`. The first four browsed Kiteworks; the next three ingested
into the local warehouse; `bpd_clear_cache` was **deleted rather than stubbed**
on purpose — a no-op "clear cache" is worse than none, because a user who calls
it reasonably believes state was reset and then reads normal results as a
failed reset.

### `bpd_health_check`

Four local checks (`bq_credentials_present`, `location_configured`,
`config_validity`, `mcp_self_check`) then eight BigQuery-backed ones:
`bq_reachable_as`, `bq_datasets_reachable`, `registry_tables_resolve` (every
logical body dry-runs clean, every base table exists, `data_grain` still holds
only the three expected values), `roles_resolvable`, `datasets_have_data`,
`feed_freshness`, `known_unpopulated_columns`, `tools_smoke_test`.

`skip_network=true` runs only the four local checks. The smoke test **dry-runs**
by default: each tool's SQL is compiled and priced by BigQuery but not executed,
which proves every dialect translation and every resolved column name at
**0 bytes billed**. Pass `execute=true` to really run them.

`roles_resolvable` is the most valuable check in the suite now — the registry's
projection is a second thing that can drift away from `COLUMN_ROLES`, and this
catches both sides.

---

## Column-role registry

Real Target schemas use non-obvious column names (`sale_quantity` not `units`,
`sales_date` not `date`, `selected_forecast_q` not `forecast_units`,
`fiscal_week_begin_d` not `week_start_date`). No analytics tool hardcodes a
column: they call `resolve_column(warehouse, table, role)` in
`src/bpd_mcp/column_roles.py`, which matches an ordered candidate list per
`(logical_table, role)` against the live schema. To handle a new Target
column-name variant, append it to the relevant
`COLUMN_ROLES["<table>"]["<role>"]` list. Errors name the candidates tried and
the columns actually present, so the fix is usually a one-line append.

That indirection is what made swapping the entire data layer tractable.

`DATASET_KINDS` classifies each table `transactional` or `dimensional`, so
`bpd_data_freshness` can compute the business-data range without
`location_attr.last_remodel_date` (which reaches back to 2000) skewing it.

---

## Cost model

BigQuery bills by byte scanned, so the server treats cost as a correctness
concern:

* **Every job carries `maximum_bytes_billed`** (`BPD_BQ_MAX_BYTES_BILLED`,
  20 GiB). An over-limit job is rejected by BigQuery as an **HTTP 500** with
  `reason: bytesBilledLimitExceeded` — not a 403 — and is surfaced as
  `QUERY_TOO_EXPENSIVE`.
* **`bpd_run_sql` and `bpd_export_query_to_csv` dry-run first.** A dry run
  validates the SQL and returns `total_bytes_processed` at 0 bytes, so it
  replaces DuckDB's `EXPLAIN` gate and adds a cost gate on top.
  `estimated_bytes_scanned` is echoed into every response.
* **Only referenced CTEs are injected.** Blanket injection would add ~400 MB of
  avoidable scan per call.
* **Metadata is free or cached.** Schemas come from cached dry runs (0 bytes);
  row counts from `__TABLES__` (0 bytes, 300 s TTL); the date-range sweep is one
  combined `UNION ALL` job (~527 MB, 900 s TTL). `INFORMATION_SCHEMA` bills a
  10 MB minimum per query and is avoided.
* **Date predicates matter.** Partition pruning survives CTE injection
  (`sales_daily` 13.3 MB → 3.5 MB with a `WHERE`), so never drop a date filter
  as a "simplification". The three `biom_canvas` facts are partitioned and
  clustered; all 13 `bpd_raw` tables are neither.

Every query logs `bytes_billed`, `bytes_processed`, `cache_hit`, the injected
logical tables and the job id at INFO.

Slower than DuckDB, unavoidably: each query is a network round trip
(~0.7–1.5 s) where DuckDB was sub-millisecond, and a tool that issues several
queries takes seconds. That is the price of the multi-process capability.

---

## Target schema quirks

Worth knowing before writing custom SQL — bugs hide here:

- **Week anchors disagree.** `forecast_weekly.fiscal_week_begin_d` is Sunday-anchored (100% of rows); every weekly sales/inventory/GM date is the Saturday week-END (100% of rows). `bpd_get_forecast_vs_actual` normalizes by shifting the forecast +6 days. A manual join must do the same.
- **Week bucketing is Monday-anchored, deliberately.** `get_sales_summary(grain='week')` uses `DATE_TRUNC(x, WEEK(MONDAY))`. BigQuery's bare `WEEK` defaults to **Sunday**; DuckDB's `date_trunc('week', …)` was Monday. The Monday form is **bug-for-bug parity** with the pre-migration numbers, not a claim about Target's fiscal calendar — a data-layer swap must not move a reported figure. It is a recorded follow-up, not a settled design; see "Follow-ups".
- **`item_attr` is in EAV form** — attributes are rows (`mta_n`, `mta_value_n`), not columns. Pivot in your query.
- **`""` (two literal double-quote characters) is Target's NULL placeholder.** It survives into BigQuery as a STRING value. `daily_order_tcin_loc.ITEM_CHANGE_D` / `IMPORTS_IN_STORE_D` / `RECEIPT_D` and `location_attr.last_remodel_date` (574 of 2,222 rows) all carry it. **Always `SAFE_CAST(col AS DATE)`, never `CAST`** — a plain `CAST` returns a hard 400 `Invalid date`, and worse, it is optimizer-dependent: `COUNT(CAST(…))` can succeed because BigQuery elides the cast, so a passing smoke query proves nothing.
- **`orders_daily.purchase_order_active_f` is 98% placeholder.** Do not filter on it; `bpd_get_open_orders` derives openness arithmetically instead.
- **`data_grain` is an unconstrained STRING.** `fct_target_inventory` has only `daily` and `history_weekly` — there is no `weekly` grain. A new fourth value would be silently ignored by the weekly tables, which is why `registry_tables_resolve` guards it.
- **`dim_product.current_price` is NUMERIC.** `NUMERIC * 1.0` stays NUMERIC with 9-digit division rounding rather than promoting to FLOAT64. No role reaches it today; if one ever does, check the precision.
- **Division guards are load-bearing.** The `NULLIF(…, 0)` and `CASE WHEN COALESCE(SUM(…),0)=0 THEN NULL` patterns look like removable boilerplate. DuckDB returned `inf` for `1/0`; BigQuery **fails the entire query**. Hold new division to the same standard.

---

## Security model

* **Read-only at the credential layer.** The service account holds `dataViewer`
  + `jobUser`. `CREATE VIEW` and `CREATE TABLE` both return 403
  `bigquery.tables.create denied`. This is strictly stronger than the
  `BEGIN TRANSACTION READ ONLY` facade it replaces — it cannot be bypassed by a
  code path, because the permission does not exist.
* **`sql_safety.py` is kept as defense in depth.** It admits only statements
  leading with `SELECT` or `WITH`, and rejects multi-statement input plus
  DDL/DML tokens including comment-cloaked variants. It is deliberately
  conservative: `REPLACE` is blocked, which also blocks BigQuery's legitimate
  `SELECT * REPLACE(...)` modifier. False positives are preferred to false
  negatives.
* **Cost is a safety property too** — see the hard `maximum_bytes_billed` and
  the dry-run gate above.
* **Secrets are never logged.** A structlog processor recursively redacts keys
  matching `(?i)(password|secret|token|authorization|bearer|refresh)`. The
  service-account key is read from `os.environ` and never becomes a settings
  field: a `SecretStr` would still reach logs via `model_dump()`.
* **No write path to anything.** No uploads, no ingestion, no DDL.
* **stdout is reserved for MCP protocol.** All logging goes to stderr and the
  rotating log file. One stray `print` on stdout corrupts the transport.

---

## Logging

Configured by `BPD_LOG_LEVEL` (default `INFO`). Two sinks plus a hard rule:

* **stderr** — JSON-rendered structlog events. Safe for stdio MCPs.
* **`~/.bpd-mcp/logs/bpd-mcp.log`** — rotating JSON, 10 MB × 5 backups.
* **Nothing to stdout, ever.**

Every tool call logs `tool_called` (arguments, redacted) and `tool_complete`
(duration). Every BigQuery job logs its bytes and job id.

---

## Tests

```bash
# Tier 1 only: hermetic — no network, no credentials, no cost.
uv run pytest -q -m "not bq and not bq_live"

# Tier 2: live BigQuery, fixture rows injected as literal CTEs (0 bytes billed).
uv run pytest -q -m bq

# Tier 3: live BigQuery against REAL production data (bills bytes).
uv run pytest -q -m bq_live

# All three. With a credential present this really does run tiers 2 and 3;
# without one it collapses to tier 1 (see the skip rule below).
uv run pytest -q
```

Three tiers, and the split is deliberate:

* **Tier 1 — hermetic.** Pure string-in/string-out tests for CTE injection,
  identifier quoting, the dialect helpers, `sql_safety`, and role resolution
  against a stub warehouse that implements nothing but `registry` and
  `logical_schema`. It also holds the **drift guards**
  (`tests/test_audit_drift_guards.py`), which pin the registry against every
  parallel source of truth that can silently disagree with it:
  `LOGICAL_TABLES` ↔ `COLUMN_ROLES` ↔ `DATASET_KINDS` ↔ `FEED_KINDS` ↔
  `schemas.KnownDataset` ↔ `bq.KNOWN_DATASET_NAMES`; `EXPECTED_TOOL_COUNT` and
  the tool roster ↔ the tools actually registered on the FastMCP instance; each
  entry's declared `date_column` and `column_contract` ↔ the columns its body
  really projects; and `REQUIRED_ROLES` ↔ `COLUMN_ROLES`. Adding a logical table
  without its companion entries fails here rather than in production.
* **Tier 2 — `-m bq`, fixture data.** Fixture rows are swapped into the registry
  as literal `SELECT … UNION ALL SELECT …` CTE bodies and executed **against
  real BigQuery**, which bills 0 bytes and runs in about 0.7 s per query. This
  is where the bulk of the analytics coverage lives — the SQL is compiled and
  computed by the engine that will run it in production, over rows whose right
  answer is known. A few drift guards also run here, comparing the projection
  parsed out of each registry body against the schema BigQuery itself reports.
* **Tier 3 — `-m bq_live`, real data.** Deliberately small: the full
  `bpd_health_check` runner against production, the registry/roles checks
  against live schemas, and the KMG tie-out below driven end to end as a
  subprocess.

Both BigQuery tiers **skip themselves** when neither `GCP_SA_KEY_B64` nor
`GOOGLE_APPLICATION_CREDENTIALS` is set, so a contributor without warehouse
access still gets a meaningful green run instead of a wall of errors.
`scripts/verify_install.sh` runs tier 1 only, for that reason.

**There is deliberately no DuckDB test double.** It was measured and rejected:
DuckDB rejects `SAFE_CAST` and backtick identifiers outright, and where both
engines accept the same text they disagree — `SELECT "sale_quantity"` returns
the column in DuckDB and the string `'sale_quantity'` in BigQuery. A double
would pass exactly when production is broken.

---

## Source-of-truth tie-out (KMG POS report)

`scripts/validate_kmg.py` validates the BigQuery data layer against the vendor's
weekly KMG POS report. That report is external to both the old DuckDB warehouse
and the new one, which is what makes this the **acceptance gate for the
migration**: it is the only artifact in the repo that can answer "did the
data-layer swap change any number?" against a ruler the swap cannot move.

It embeds the numbers from the JunW1'26 report (week ending 2026-06-06) and runs
**62 checks**: weekly unit and dollar totals for 12 fiscal weeks, per-TCIN units
and dollars for w/e 6/6 (25 SKUs), the channel-originated dollar split and its
reconciliation to the weekly total, and per-TCIN on-hand for the 21 SKUs whose
reported OH is above noise level.

```bash
uv run python scripts/validate_kmg.py --project biom-reporting-s26 --location us-central1
```

Read-only. Four queries and a few seconds; **94 MB billed** on a cold query
cache, 0 on a repeat run inside the cache window. No column name is
hardcoded — it resolves through `column_roles.resolve_column` and
`ResolvedColumn.select_as_date`, so it fails the same way the tools would if
Target renames something. Exit 0 = nothing failed; `--strict` additionally fails
on the documented report-side discrepancy below.

### Result of the post-migration run

**2026-09-01, against production: 61 passed, 1 known-discrepancy, 0 failed,
0 skipped.** The swap moved no number.

* All 12 weekly dollar totals tie, and 11 of the 12 weekly unit totals tie — the
  largest of those differences is 0.03%.
* All 25 per-SKU unit figures for w/e 6/6 match the report exactly, and all 25
  dollar figures match to the report's whole-dollar rounding.
* The channel split reconciles: store-originated reads 0.49% low and
  online-originated 1.22% high (offsetting: -$1,159 / +$1,202), while their
  **total** matches the report to 0.013%.
* All 21 on-hand figures match, 12 of them against `ending_on_hand_q` alone and
  the other 9 only once `ending_on_transfer_q` is added — KMG's "OH" column
  includes in-transit units for some SKUs and not others. The harness accepts
  either definition and prints which one matched.

The single outstanding difference is `w/e 2026-04-04` units, and it is a defect
in the report rather than in the data:

| source | units | dollars |
| --- | --- | --- |
| KMG report, "12 Week Comparison" | 21,352 | $213,179 |
| `biom_canvas.fct_target_sales` (`data_grain='weekly'`) | **23,994** | $213,175.31 |
| `bpd_raw.weekly_sales_tcin_loc` | **23,994** | $213,175.31 |
| `bpd_raw.history_sales_weekly` | **23,994** | $213,175.31 |

Three independent Target feeds agree with each other and disagree with the
report's unit figure, while the report's own dollar total for that week ties to
0.002%. The last of the three is the Kiteworks file the DuckDB warehouse loaded
directly, so the pre-migration number was 23,994 as well.

The harness reports that week as `KNOWN` rather than `FAIL` — a gate that can
never go green stops being read — but the allowance is **pinned to 23,994** in
`KNOWN_REPORT_DISCREPANCIES`. It is an assertion, not a whitelist: if that week
ever returns any other number it is a plain `FAIL` again, and a tier-2 test
(`test_moving_off_the_pinned_value_is_a_real_failure`) holds that behaviour in
place.

---

## Evaluation suite

`evals/bpd_eval.xml` contains 10 realistic multi-tool questions, each written to
be answered end to end through the MCP. It is **not yet a regression suite**:
every `<answer>` is still the `__FILL_FROM_REAL_DATA__` placeholder, so nothing
can be string-compared until the expected values are pinned against real data.
The questions themselves are usable today as a manual exercise of the tools.

---

## Follow-ups (recorded, not decided)

1. **Week convention.** `DATE_TRUNC(x, WEEK(MONDAY))` is bug-for-bug DuckDB
   parity. Target's real fiscal week is Sunday-start / Saturday-end, so the
   current bucketing pushes a Saturday week-end back to the preceding Monday.
   Worth deciding `WEEK(SUNDAY)` on its own merits, separately from this swap.
   Do **not** harmonize it with the ±6-day anchoring in `get_forecast_vs_actual`
   — that is Target's fiscal week-end anchor and a different concept.
2. **The canvas weekly gap.** `fct_target_sales` stopped ingesting
   `data_grain='weekly'` after 2026-05-02 and `fct_target_inventory` never had a
   weekly grain, while the raw weekly feeds are current. The registry unions
   canvas with raw on a computed boundary, which self-heals — but the cleaner
   fix is upstream in the GCS → BigQuery loader.
3. **The DFE forecast feed is genuinely stale.** `DFE_WKLY_ITEM_LOC_FORECAST`
   last landed 2026-07-29 UTC, carrying a file dated 2026-07-27 — 12 files in
   total and nothing since (checked 2026-09-01). Not a migration bug;
   `bpd_data_freshness` just makes it visible for the first time. Note the
   content still reaches forward: fiscal weeks in `forecast_weekly` run to
   2026-10-18, which is why `bpd_list_datasets` reports snapshot and content
   ranges separately.
4. **`snapshot_retention_caveat`** in `get_forecast_vs_actual` describes
   DuckDB's per-key ingest retention. The BigQuery sources are SCD2 with
   `valid_from`/`valid_to`, so historical snapshots may be recoverable and
   `pre_week` backtesting may be genuinely possible. The caveat is now likely
   wrong in the user's favour. Unverified.
5. **Two roles resolve to nothing** — `gross_margin.margin` and
   `item_attr_extended.date`. Both were unresolvable under DuckDB too and no
   tool consumes either. Left faithful; flagged for a cleanup pass.
6. **Three dead item-grain feeds.** `sales_weekly_item`, `inventory_weekly_item`
   and `gross_margin_item` all stop at 2026-05-16. Registered so the surface
   stays complete and the staleness is visible; worth asking Target whether they
   are retired.
7. **`.env.example` still contains committed Kiteworks credentials.** Rotating
   them was explicitly deferred; the file carries a TODO where they were.
