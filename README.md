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

# 2. One-time interactive auth bootstrap (saves a refresh token to ~/.bpd-mcp/tokens.json @ 0600)
uv run bpd-bootstrap

# 3. Run the MCP server (stdio transport — for Claude Desktop / Claude Code)
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

### Admin

| Tool               | Purpose                                                                                   |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `bpd_auth_status`  | OAuth state, scope, expires_in_s, user email (via `/rest/users/me`).                       |
| `bpd_cache_status` | Disk usage, row counts, oldest/newest data dates, last sync time.                          |
| `bpd_clear_cache`  | **Destructive.** Requires `confirm=true`. Otherwise returns a dry-run preview.            |

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
              | /rest/folders/top   |         | bpd.duckdb             |
              | /rest/folders/{}/.. |         |  ├ sales_daily         |
              | /rest/files/{}/..   |         |  ├ sales_weekly        |
              +---------------------+         |  ├ inventory_*         |
                                              |  ├ item_attr           |
                                              |  ├ location_attr       |
                                              |  ├ gross_margin        |
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
* `tests/test_warehouse.py` — idempotent loads, schema drift, view creation.
* `tests/test_sql_safety.py` — keyword/AST blocks AND engine-level read-only enforcement.
* `tests/test_tools_query.py` — sales_summary math + markdown/json toggle.

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
