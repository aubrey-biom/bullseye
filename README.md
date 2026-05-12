# bpd-mcp — Target BPD MCP Server

A local stdio MCP server that lets Claude work with Target's **Business Partner Data**
(daily/weekly sales, inventory, item attributes, location attributes, gross margin)
delivered through the vendor's **Kiteworks** drop folder. Files are downloaded over
the Kiteworks REST API, parsed, and loaded into a local **DuckDB** warehouse that
Claude can query with SQL or with the prebuilt analytics tools below.

The server is written in Python (3.11+). Framework: **FastMCP**. Data layer:
DuckDB + Polars. Auth: OAuth2 password grant + refresh token against
`{base_url}/oauth/token`.

---

## Quickstart

```bash
# 0. Install
pip install uv               # if you don't have it
uv sync                      # creates .venv and installs everything

# 1. Configure
cp .env.example .env
$EDITOR .env                 # set KITEWORKS_USERNAME / KITEWORKS_PASSWORD / BPD_VENDOR_ID

# 2. Verify the install is healthy (one command; no MCP needed).
./scripts/verify_install.sh

# 3. One-time interactive auth bootstrap (saves a refresh token to ~/.bpd-mcp/tokens.json @ 0600)
uv run bpd-bootstrap

# 4. Run the MCP server (stdio transport — for Claude Desktop / Claude Code)
uv run bpd-mcp
```

Once running, point Claude Desktop at it:

```json
{
  "mcpServers": {
    "bpd": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/bpd-mcp", "run", "bpd-mcp"],
      "env": {
        "KITEWORKS_USERNAME": "you@biom.com",
        "KITEWORKS_PASSWORD": "...",
        "BPD_VENDOR_ID": "139440",
        "BPD_VENDOR_TIER": "BV"
      }
    }
  }
}
```

Then in Claude: ask "sync new files" → "describe the schema" → "what were
trailing-4-week unit sales?". The tool reference is below.

---

## Configuration

Configuration is read from environment variables and (optionally) a `.env` file at
the project root. See `.env.example` for the full list. The non-obvious knobs:

| Var                          | Default                             | Notes                                                                 |
| ---------------------------- | ----------------------------------- | --------------------------------------------------------------------- |
| `KITEWORKS_BASE_URL`         | `https://securesharek.target.com`   | Host pinned at the HTTP layer — only this host is allowed.            |
| `KITEWORKS_CLIENT_ID`        | (Target's shared ID)                | Pre-filled with the credentials from the BPD setup PDF.               |
| `KITEWORKS_CLIENT_SECRET`    | (Target's shared secret)            | Pre-filled. If Target rotates these, update `.env`.                   |
| `KITEWORKS_OAUTH_SCOPE`      | `*/*/*`                             | If Kiteworks rejects, the error message is surfaced verbatim — try `folders/* files/* search/* users/me`. |
| `KITEWORKS_API_VERSION`      | `15`                                | Sent as `X-Kiteworks-Version` on every REST call.                     |
| `BPD_VENDOR_ID`              | `139440`                            | The Kiteworks folder name (Biom's BPID).                              |
| `BPD_VENDOR_TIER`            | `BV`                                | `BV` Basic, `BR` Brand, `CC` Category Captain.                        |
| `BPD_DATA_DIR`               | `~/.bpd-mcp`                        | Root for raw zips, the DuckDB warehouse, tokens, and logs.            |
| `BPD_AUTO_SYNC_ON_START`     | `false`                             | If true, sync new files when the MCP starts.                          |
| `BPD_MAX_PARALLEL_DOWNLOADS` | `4`                                 | Concurrency cap for the sync worker.                                  |

The data dir layout is:

```
~/.bpd-mcp/
├── raw/                  # downloaded .zip files (LRU-capped at 5 GB)
├── extracted/            # transient unzip workspace
├── bpd.duckdb            # the warehouse
├── bpd.duckdb.ro         # read-only snapshot for `bpd_run_sql` (auto-managed)
├── tokens.json           # 0600 perms enforced
└── logs/bpd-mcp.log      # rotating JSON log (10 MB × 5)
```

---

## Tool reference

Every tool name is prefixed `bpd_` to avoid collision with other MCPs. Every tool
accepts a `response_format` of `markdown` (default) or `json`.

### Discovery & files

| Tool                       | Purpose                                                              |
| -------------------------- | -------------------------------------------------------------------- |
| `bpd_list_top_folders`     | List Kiteworks top-level folders. Use once to find your BPID folder. |
| `bpd_list_folder_contents` | Paginated children of a folder. Supports `name_contains`, `extensions`. |
| `bpd_get_file_metadata`    | Size, fingerprint, dates, parent.                                    |
| `bpd_search_files`         | Wraps `/rest/query` for ad-hoc filename / content search.            |

### Sync

| Tool                  | Purpose                                                                       |
| --------------------- | ----------------------------------------------------------------------------- |
| `bpd_sync_new_files`  | Discover new BPD zips → download → parse → load. Idempotent. Supports `dry_run`. |
| `bpd_refresh_dataset` | Re-load a single dataset; `full=true` clears+rebuilds.                        |
| `bpd_list_datasets`   | Row count, min/max data date, file count, last-loaded time per dataset.       |

### Query

| Tool                         | Purpose                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| `bpd_run_sql`                | Arbitrary DuckDB SQL. **Read-only enforced at the engine layer** (separate `read_only=True` connection on a snapshot copy of the DB) AND at the validator (multi-statement and DDL/DML tokens rejected, comment-cloaked included). Wraps the result in `LIMIT N`. |
| `bpd_describe_schema`        | All tables, columns, types. Also exposed as MCP resource `bpd://schema`.    |
| `bpd_get_sales_summary`      | Sum units (and dollars when available) by `day`/`week`/`month` with optional filters. |
| `bpd_get_top_skus`           | Top-N SKUs by units or dollars over a date range.                           |
| `bpd_get_inventory_snapshot` | Latest known on-hand per TCIN × location at or before a date.               |
| `bpd_get_sell_through`       | Joins sales + latest inventory to compute weeks-of-supply + sell-through.   |

### S&OP analytics (May 2026 patch)

| Tool                          | Purpose                                                                                  |
| ----------------------------- | ---------------------------------------------------------------------------------------- |
| `bpd_get_open_orders`         | Outstanding Target POs summed by SKU. Uses an "open/remaining" qty col if present; else excludes statuses that look fulfilled/closed; else sums all ordered units placed ≤ `as_of_date`. Method chosen is reported in `extra.method`. |
| `bpd_get_upcoming_pos`        | UNION of `po_plan_daily` + `po_plan_biweekly`, grouped by week. `weeks_forward` (default 8) is anchored at today. |
| `bpd_get_forecast_vs_actual`  | Joins Target's DFE `forecast_weekly` against `sales_weekly`. Returns forecast/actual/variance per group; `aggregate` picks `by_sku_week` (default), `by_sku_location_week`, or `by_sku`. |

### Admin

| Tool                       | Purpose                                                                                   |
| -------------------------- | ----------------------------------------------------------------------------------------- |
| `bpd_auth_status`          | OAuth state, scope, expires_in_s, user email (via `/rest/users/me`).                       |
| `bpd_cache_status`         | Disk usage, row counts. Reports two date ranges: `earliest/latest_data_date` (transactional datasets only — the business-data range) and `earliest/latest_data_date_including_dimensional` (covers `location_attr.last_remodel_date` etc.). Per-dataset breakdown includes the detected date column and dataset `kind`. |
| `bpd_clear_cache`          | **Destructive.** Requires `confirm=true`. Otherwise returns a dry-run preview.            |
| `bpd_health_check`         | 14-check audit across auth, warehouse, sync ledger, disk, MCP self-state. Each returns pass/warn/fail. Use as the first call when diagnosing any MCP issue. Set `skip_network=true` for offline mode. |
| `bpd_export_query_to_csv`  | Run a read-only SQL query and write the result to `~/.bpd-mcp/exports/<filename>` (mode 0644). Useful for sharing data with team members who don't have MCP access. Same read-only safety as `bpd_run_sql`. |

---

## Column-role registry (Patch #4)

Real Target schemas use non-obvious column names (`sale_quantity` not `units`,
`sales_date` not `date`, `selected_forecast_q` not `forecast_units`,
`fiscal_week_begin_d` not `week_start_date`). The analytics tools (and the
`forecast_vs_actual` snapshot logic) resolve column names dynamically at call
time via `src/bpd_mcp/column_roles.py`. To handle a new Target column-name
variant, append it to the relevant `COLUMN_ROLES["<dataset>"]["<role>"]` list.
Errors include the candidates tried and the actual columns present, so the fix
is usually a one-line append.

`DATASET_KINDS` classifies each dataset as `transactional` or `dimensional` —
used by `bpd_cache_status` to compute the "business data" date range without
dimensional date columns (e.g. `last_remodel_date` back to 2000) skewing it.

`bpd_get_forecast_vs_actual` accepts `as_of_date` to lock the forecast snapshot
cutoff. When omitted, the default is "the day before each forecast week begins"
— giving you the pre-week prediction Target actually published, not a post-hoc
revised one. The tool picks the latest `last_update_d` ≤ cutoff per
`(tcin, location, week)`.

---

## Target schema quirks

Worth knowing when writing custom SQL against the warehouse — bugs hide here:

- **Week anchors disagree.** `forecast_weekly.fiscal_week_begin_d` is Sunday-anchored; `sales_weekly.sales_date` is the Saturday week-end. `bpd_get_forecast_vs_actual` normalizes both to Saturday (shifts the forecast +6 days) for joining. If you write a manual join via `bpd_run_sql`, do the same.
- **`item_attr` is in EAV form** — item attributes are rows (`mta_n`, `mta_value_n`), not columns. Pivot to wide form in your query if you need attributes side-by-side.
- **`sales_weekly.sales_date` is the week-end Saturday**, not a generic sales date. Don't filter as if it's a "transaction occurred on this day" column.
- **`forecast_weekly` ships dates as VARCHAR.** `fiscal_week_begin_d` is stored as text like `'2026-05-03'`. The analytics tools insert a `CAST(... AS DATE)` at query time automatically; manual SQL needs the same cast.
- **`forecast_weekly` carries multiple snapshots per (tcin, location, week)** distinguished by `last_update_d`. Use `bpd_get_forecast_vs_actual`'s `as_of_date` (or `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY last_update_d DESC)`) to pick a single snapshot.
- **Product names contain unescaped inch marks** (e.g. `6"` in the Bone SKU). The parser uses `quote_char=None` to handle this. If you ever write a custom CSV reader against the raw BPD files, do the same.

---

## Warehouse design (Patch #3)

The MCP keeps **one** `~/.bpd-mcp/bpd.duckdb` file. Engine-level read-only execution
for `bpd_run_sql` is provided by wrapping each query in `BEGIN TRANSACTION READ ONLY`
on a fresh cursor against the writable connection. DuckDB rejects writes inside
such a transaction at the engine layer (verified on DuckDB 1.5.2).

This replaces the earlier `.duckdb.ro` snapshot file, which caused schema-drift
bugs whenever migrations applied to the writable copy weren't reflected in the
snapshot. On startup, the server now deletes any leftover `.duckdb.ro` /
`.duckdb.ro.wal` from prior installs (look for `removed_legacy_snapshot` events
in the log).

**Defense in depth for `bpd_run_sql`:**
1. Validator (app layer): rejects multi-statement, DDL/DML keywords, comment-cloaked
   writes, `ATTACH`, `COPY`, `INSTALL/LOAD`, `EXPORT DATABASE`.
2. Engine (DuckDB layer): `BEGIN TRANSACTION READ ONLY` rejects any write that
   touches the current database.

Note: DuckDB's read-only transaction does NOT cover `ATTACH` / `COPY ... TO` —
those are stopped at the validator layer instead. The two layers together cover
every write surface.

---

## How the data flows

```
+-------------------+         +------------------------+
| Claude            | stdio   | bpd-mcp (Python)       |
|                   |<------->| FastMCP + tools        |
+-------------------+         | Auth manager           |
                              | Sync worker            |
                              +-----------+------------+
                                          |
                          +---------------+----------------+
                          |                                |
                          v                                v
              +---------------------+         +------------------------+
              | Kiteworks REST API  |         | Local data layer       |
              | /oauth/token        |         | raw/ (zip audit trail) |
              | /rest/folders/top   |         | bpd.duckdb (15 tables) |
              | /rest/folders/{}/.. |         |  ├ sales_daily         |
              | /rest/files/{}/..   |         |  ├ sales_weekly[_item] |
              +---------------------+         |  ├ inventory_daily     |
                                              |  ├ inventory_weekly[_item]
                                              |  ├ gross_margin[_item] |
                                              |  ├ item_attr[_extended]|
                                              |  ├ location_attr       |
                                              |  ├ orders_daily        |
                                              |  ├ po_plan_daily       |
                                              |  ├ po_plan_biweekly    |
                                              |  ├ forecast_weekly     |
                                              |  ├ _file_ledger        |
                                              |  ├ _sync_log           |
                                              |  └ _schema_registry    |
                                              +------------------------+
```

The sync worker:

1. Finds the vendor's top-level folder by matching its name to `BPD_VENDOR_ID`.
2. Walks the folder (depth-capped) and collects every file.
3. Classifies each file name against the BPD pattern catalog (`parsers.py`).
4. Skips files already loaded with a matching fingerprint (per `_file_ledger`).
5. For each new file: streams the download to `raw/`, opens the inner pipe/tab-delimited
   text (delimiter sniffed from line 1), reads with Polars, registers the discovered
   schema in `_schema_registry`, then INSERT-OR-REPLACE on the natural primary key into
   the discovered DuckDB table.
6. Emits a structured summary back to the MCP tool caller.

`-1` is preserved as a meaningful integer sentinel ("not applicable") rather than
NULL — Target uses it in their data model and silently coercing would lose meaning.

Schema drift (new columns) is logged with the diff. The data table is not auto-altered
in v1; you can re-run `bpd_refresh_dataset` with `full=true` if a real column was added.

### Parse resilience (May 2026 patch)

Target sometimes ships malformed files (extra delimiters, embedded quotes, BOM,
mixed line endings). The parser uses a three-tier fallback chain and records
which tier succeeded:

1. **`strict`** — polars `read_csv` with strict parsing. The happy path.
2. **`ignore_errors`** — polars with `ignore_errors=True`. Rows that fail
   tokenization are skipped; the file still loads.
3. **`pandas_permissive`** — pandas python engine with
   `on_bad_lines='skip'`. Slower but tolerates everything but binary garbage.

`_file_ledger` has two diagnostic columns to expose what happened:

- **`parse_method`** ∈ `{strict, ignore_errors, pandas_permissive, failed}`
- **`error_message`** — full exception text (truncated only at 2000 chars).
  Populated on both failures and on fallback successes (so you can find files
  that loaded *but* needed permissive parsing).

Useful query: `SELECT file_name, dataset, status, parse_method, error_message
FROM _file_ledger WHERE parse_method != 'strict' OR status = 'failed'`.

The `bpd_cache_status` tool reports overall earliest/latest data dates *and*
a per-dataset breakdown showing the detected date column and date range per
table. Date columns are discovered by type (DATE/TIMESTAMP) first, then by
name heuristic (`*_date`), then by a per-dataset fallback registry.

---

## Security model (§15)

* **Token file is 0600.** The server refuses to start if perms are looser.
* **Secrets are never logged.** A structlog processor recursively redacts any key
  matching `(?i)(password|secret|token|authorization|bearer|refresh)`. Tokens that
  do get logged (e.g. on acquisition) are masked as `<token:1234...abcd>`.
* **Outbound calls are host-pinned** to `KITEWORKS_BASE_URL` at the `httpx` event-hook
  layer. Any attempt to talk to another host raises immediately.
* **`bpd_run_sql` is read-only.** Enforced at the engine layer (the connection is
  opened `read_only=True` against a snapshot of the DB) AND at the validator
  (multi-statement, DDL, DML, ATTACH, COPY, INSTALL/LOAD, PRAGMA-with-assignment all
  rejected — and comment-cloaked variants too).
* **No write APIs to Kiteworks.** Uploads/deletes/share are not implemented.
* **stdout is reserved for MCP protocol.** All logging goes to stderr + the rotating
  log file. A single stray print on stdout would corrupt the MCP transport.

---

## Logging

Configured by `BPD_LOG_LEVEL` (default `INFO`). Three sinks:

* **stderr** — JSON-rendered structlog events. Safe for stdio MCPs.
* **`~/.bpd-mcp/logs/bpd-mcp.log`** — rotating JSON, 10 MB × 5 backups.
* **Nothing to stdout, ever.**

Every tool call logs a `tool_called` event with its arguments (after redaction) and a
`tool_complete` event with duration. Sync worker emits per-file events.

---

## Tests

```bash
# Unit + integration (no network)
uv run pytest -q

# With the real Kiteworks (requires creds in env; never run in CI)
BPD_INTEGRATION=1 uv run pytest -q -k integration
```

Coverage requirements (§13):

* `tests/test_auth.py` — password→refresh transition, 0600 perms, error surface verbatim.
* `tests/test_parsers.py` — filename catalog, pipe/tab sniff, `-1` sentinel, schema discovery.
* `tests/test_warehouse.py` — idempotent loads, schema drift, view creation, migration idempotency.
* `tests/test_sql_safety.py` — keyword/AST blocks.
* `tests/test_read_only_view.py` — engine-level RO enforcement, legacy snapshot cleanup, migration visibility.
* `tests/test_health_check.py` — every health check has pass + fail tests.
* `tests/test_audit_drift_guards.py` — pin parallel sources of truth (PATTERNS ↔ KnownDataset, EXPECTED_LEDGER_COLUMNS ↔ DDL, EXPECTED_TOOL_COUNT ↔ registered tools).
* `tests/test_tools_query.py` — sales_summary math + markdown/json toggle.

---

## Post-patch verification sequence

After every patch lands, the user runs:

```bash
git pull && uv sync
pkill -f bpd-mcp && sleep 2
rm -f ~/.bpd-mcp/bpd.duckdb.ro ~/.bpd-mcp/bpd.duckdb.ro.wal   # one-time, patch #3
./scripts/verify_install.sh                                   # local checks (8 steps)
# Fully quit + reopen Claude Desktop
# In Claude Desktop: call bpd_health_check                    # 14-check audit
# In Claude Desktop: call bpd_sync_new_files                  # ~99 loaded, 0-2 failed
```

`verify_install.sh` validates the local install (imports, tests, ruff, ledger schema,
token perms, tool count) without touching the network. `bpd_health_check` runs once
the MCP is back up and adds the cross-cutting checks (auth, RO enforcement, sync
ledger invariants, orphan files, etc.).

---

## Evaluation suite

`evals/bpd_eval.xml` contains 10 realistic Q&A pairs. The `<answer>` values are
placeholders until Aubrey runs the MCP against her real data and pins them. Re-run
after every meaningful change — they're the regression suite.

---

## Known limits & next steps (§16, §19)

* **OAuth endpoint is `/oauth/token`** per Kiteworks docs and the BPD PDF. If a 404
  ever appears at that path, the exact error is surfaced — the server does NOT
  silently probe alternate paths.
* **Client credentials are shared across all BPD vendors.** If Target rotates them,
  update `KITEWORKS_CLIENT_ID` / `KITEWORKS_CLIENT_SECRET` in `.env`.
* **Refresh token TTL is server-side.** If the server hasn't refreshed before TTL,
  `bpd-bootstrap` must be re-run.
* **`bpd_run_sql` reads a snapshot of the warehouse**, refreshed lazily on each call
  (mtime check). This is the engine-level read-only handle the spec asks for —
  DuckDB cannot mix RW/RO connections to the same file in one process.
* **No file upload / share / member endpoints.** v1 is read-only by design.
* **Watch mode, remote deployment, materialized rollups** — see §16. Not in v1.
