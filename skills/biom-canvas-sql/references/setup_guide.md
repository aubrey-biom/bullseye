# BigQuery Setup Guide — BIOM CANVAS
**Project:** `biom-reporting-s26` · **Region:** `us-central1` · **Primary dataset:** `biom_canvas`

**Two environments connect two different ways. Pick yours:**

- **Claude Code** (web or cloud session) — a service account is already wired up.
  Nothing to install, no browser login. **Read Part A, skip Part B entirely.**
- **Your own Mac terminal** — browser login as yourself. **Part B.**

---

# Part A — Claude Code (service account)

There is no `gcloud` and no `bq` CLI in a Code container, and no browser to log
in with. Auth comes from the `GCP_SA_KEY_B64` environment variable — a
base64-encoded key for
`claude-code-bq-readonly@biom-reporting-s26.iam.gserviceaccount.com` — and
queries go through the BigQuery Python client.

```python
import base64, os
from pathlib import Path
from google.cloud import bigquery

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    b64 = os.environ["GCP_SA_KEY_B64"]
    dest = Path.home() / ".config" / "gcloud" / "biom-bq-sa.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))
    dest.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(dest)

client = bigquery.Client(project="biom-reporting-s26", location="us-central1")
sql = "SELECT COUNT(*) n FROM `biom-reporting-s26.biom_canvas.fct_orders` WHERE is_current"
print(list(client.query(sql).result())[0].n)
```

**`location="us-central1"` is not optional.** Omit it and the client defaults to
the US multi-region, where `INFORMATION_SCHEMA` returns **zero rows** — which
reads like an empty warehouse rather than a wrong region, and has already cost
real time here.

The same trap in SQL: region-qualified metadata views need the region in the path.

```sql
-- returns nothing (wrong region, no error)
SELECT schema_name FROM `biom-reporting-s26`.INFORMATION_SCHEMA.SCHEMATA
-- correct
SELECT schema_name FROM `biom-reporting-s26.region-us-central1.INFORMATION_SCHEMA.SCHEMATA`
```

### What this credential can and cannot do

- **Reads** every dataset in the project — 12 of them, not only `biom_canvas`.
- **Writes** nothing, anywhere. It holds `dataViewer` + `jobUser` and no more.
- **Cannot read customer identifiers.** See Rule 12 in `database_reference.md`.
- **Cannot list jobs** (`bigquery.jobs.listAll` is not granted), so
  `INFORMATION_SCHEMA.JOBS_BY_PROJECT` is unavailable — a human with
  `bigquery.admin` has to run job-history queries.

### No `bq` CLI here

Wherever this skill shows `bq query`, the Code equivalent is
`client.query(sql).result()`. Two `bq` conveniences need replacing by hand:

| `bq` flag | Python equivalent |
|---|---|
| `--dry_run` | `bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)`, then read `job.total_bytes_processed` |
| `--max_rows=N` | not needed — iterating `.result()` pages through everything |

---

# Part B — Your own Mac terminal

*(Skip all of Part B in Claude Code.)*

## Step 1 — Install the Google Cloud CLI

If you don't already have `gcloud` installed:

```bash
brew install --cask google-cloud-sdk
```

If you don't use Homebrew, download the installer directly from Google instead: https://cloud.google.com/sdk/docs/install

Verify it installed correctly:

```bash
gcloud --version
```

You should see `Google Cloud SDK` and a version number. `bq` (the BigQuery CLI) is bundled with this install — no separate install needed.

---

## Step 2 — Authenticate (two separate logins, both required)

BigQuery access needs **two different types of authentication** — doing only one will cause confusing failures later, so do both:

```bash
# Login 1 — for gcloud/bq CLI commands you type directly
gcloud auth login

# Login 2 — Application Default Credentials, for any Python scripts
# or tools that use Google Cloud client libraries
gcloud auth application-default login
```

Both will open a browser window — sign in with the Google account that's been granted access to this project (ask Santush if you're unsure which account to use).

---

## Step 3 — Set the active project

```bash
gcloud config set project biom-reporting-s26
```

Confirm it's set correctly:

```bash
gcloud config get-value project
```

Should print `biom-reporting-s26`.

---

## Step 4 — Verify everything works

Run a trivial, free query to confirm the whole chain works end to end:

```bash
bq query --nouse_legacy_sql --project_id=biom-reporting-s26 '
SELECT 1 AS test
'
```

If you see a small table with `test = 1`, you're fully connected.

---

## Step 5 — Your first real query

```bash
bq query --nouse_legacy_sql --project_id=biom-reporting-s26 '
SELECT table_name, table_type
FROM `biom-reporting-s26.biom_canvas.INFORMATION_SCHEMA.TABLES`
ORDER BY table_name
'
```

This lists every table and view in the main analytical dataset — a good sanity check that you can see the real schema.

---

## Everyday Usage Notes

### Running a query
```bash
bq query --nouse_legacy_sql --project_id=biom-reporting-s26 '
YOUR SQL HERE
'
```

### Checking query cost BEFORE running it (recommended habit)
BigQuery bills by data scanned. Always dry-run anything on a large table first:
```bash
bq query --nouse_legacy_sql --dry_run --project_id=biom-reporting-s26 '
YOUR SQL HERE
'
```
This prints "Bytes processed" with zero cost. If a query you expect to be small shows gigabytes, add a partition filter before running for real (see the Database Reference doc for which columns are partition keys).

### A known CLI gotcha
`bq query --format=json` **silently truncates results to 100 rows** unless you explicitly pass `--max_rows`:
```bash
bq query --format=json --max_rows=10000 --nouse_legacy_sql --project_id=biom-reporting-s26 '...'
```
This has caused real confusion on this project before (looked like a data gap; was actually just a truncated result set) — always set `--max_rows` explicitly for anything beyond a quick row count.

### Where to find the actual schema/business logic
See the companion file **`BIOM_CANVAS_Database_Reference.md`** — that's the real reference for table structure, join keys, known gotchas, and locked revenue figures. This setup guide only gets you connected; that file explains what you're looking at once you are.

---

## Optional: A GUI, if you'd rather not live in the terminal

The BigQuery web console works with the same login (visit https://console.cloud.google.com/bigquery, select the `biom-reporting-s26` project). Useful for browsing schemas visually, but the terminal `bq` CLI is what most of this project's own tooling assumes you're using.

---

## If something doesn't work

- **"Permission denied" on the project** — you likely haven't been granted IAM access yet. Ask Santush to add your Google account.
- **`gcloud` command not found after install** — restart your terminal, or run `source ~/.zshrc` (or `~/.bash_profile` depending on your shell).
- **Queries run but return nothing you expect** — check you're not missing a required filter (`is_current = TRUE` on most tables — see the Database Reference doc's Query Rules section before assuming the data is missing).
