---
name: biom-canvas-sql
description: Write, debug, and run SQL against Biom's BigQuery data warehouse (the CANVAS project, biom-reporting-s26). Use this whenever the user asks any question that needs Biom business data — revenue, orders, units, AOV, customers, retention or repurchase, Loop subscriptions, cross-sell across categories, dispenser conversion, media and ad performance, or Target/BPD retail sell-through and velocity — even if they do not say the words SQL, query, or BigQuery, and even if they just paste query output and ask what it means. In Claude Code a read-only service account already reaches the whole warehouse, so a plain-language question can go straight to a verified result and on to a deliverable (Excel, deck, artifact) with no SQL file written first. Also use when connecting for the first time, checking query cost, or reconciling a figure against locked revenue anchors. This skill carries the warehouse's correctness rules (is_current filtering, SUM-safe money columns, join keys, the certified biom_canvas layer, net-revenue source, product taxonomy, and the customer-identifier columns that are deliberately unreadable). Consult it before writing any Biom query, because almost every past mistake on this platform traces to skipping one of these rules.
---

# Biom CANVAS SQL

Helps write **correct** SQL against Biom's BigQuery warehouse (`biom-reporting-s26`) and turn the results into defensible answers. The warehouse is heavily rule-laden — the value of this skill is not knowing SQL, it's knowing *this warehouse's* traps.

**Before writing any query, open `references/database_reference.md` and read Section 1 (THE RULES).** That file is the source of truth for tables, join keys, taxonomy, and locked anchors. This SKILL.md is the *method* wrapped around it.

---

## The four-step method

Every Biom data request follows the same arc. Don't skip step 0 — it's where investor-facing rigor lives.

**Step 0 — Spec the question (judgment, not SQL).**
Translate the fuzzy ask into a precise analytical definition *before* touching SQL. Decide and state explicitly:
- **Definitions** — what exactly is a "category," a "customer," a "repurchase," "net revenue"? (These have specific meanings here — see the reference.)
- **Cohort** — fixed same-store/same-customer carry-forward, or all-entities? New launches or new doors mechanically inflate all-entity trends; a fixed cohort shows the *same* entities changing. For any "is X growing" question, default to a fixed cohort and say so.
- **Contaminants to strip** — ShopMy $0 gifting orders, $1 PR/sample SKUs, test SKUs. Name them up front.
- **Grain and channel** — Shopify (customer-level) vs Target/BPD (store-level, no customer identity). Cross-customer questions are Shopify-only.

**Step 1 — Map to certified tables + keys.**
Look it up in the reference; do not explore raw datasets. Query the **`biom_canvas`** layer only. Pick the table, confirm its grain, and confirm the join key (never SKU for Shopify products — `variant_id`; Target — `tcin` with `SAFE_CAST`).

**Step 2 — Write rule-compliant SQL.**
Run the pre-flight checklist below against the draft. This is the step where correctness is won or lost.

**Step 3 — Run, validate, assemble.**
Run with a dry-run cost check first if the table is large. Reconcile the output against the locked revenue anchors (reference Section 6) — *if a query disagrees with an anchor, the query is wrong, not the anchor.* Then assemble the answer with the step-0 caveats stated plainly.

---

## Pre-flight checklist (verify BEFORE handing the user a query)

These are the highest-frequency failure modes. The full rule set is reference Section 1 — this is the short list that catches most bugs. If any box can't be checked, fix the query first.

1. **Layer** — querying `biom_canvas`, not `biom_raw` / `biom_core` / `biom_marts` / `bpd_curated`? The certified layer is `biom_canvas`.
2. **`is_current = TRUE`** — present on every **base table** in the query? And *absent* on views (`vw_*`), which already filter internally and will double-filter if you add it?
3. **Money at line grain** — not SUM-ing order-level columns (`current_total_price`, `current_subtotal_price`, `current_total_discounts`, `total_shipping`, `total_tax`) on the line-grain `fct_orders`? Use `gross_using_line_price` / `net_line_sales`, which are SUM-safe.
4. **Join keys** — Shopify products joined on `variant_id` (never `sku` — duplicate-SKU pattern exists); Target on `tcin` with `SAFE_CAST` (types differ by table)?
5. **Net revenue** — using `admin_net_revenue` (lives *only* in `vw_revenue_subscriptions`), not `net_revenue_amount` (refunds not subtracted)?
6. **Refunds** — if a true-net figure is needed, subtracting `fct_refunds` with `CAST(order_id AS STRING)` (type mismatch)?
7. **Subscription status** — column is `status`, not `subscription_status`, on `fct_subscriptions`?
8. **Category** — using the maintained `dim_product.product_category` / `product_sub_category`, NOT regex-matching SKU strings? (SKU-pattern logic is already locked into the taxonomy — see reference Section 4.)
9. **Keyless lines** — if the query classifies products, is it handling the ~54K `fct_orders` lines where `variant_id IS NULL`? These include *active* flagship kits and dispensers that never join to `dim_product` — a join-only classification silently drops them. Use the two-tier resolver (taxonomy join + `product_title` fallback) and exclude non-product titles. See reference Section 9.
10. **Channel scope** — if the question is about D2C customers, is `order_source` filtered to core D2C (`web` + `subscription_contract*`), excluding wholesale/marketplace/POS? See reference Section 9.4.
11. **Time zone** — Central Time (`order_created_datetime_ct`) when reconciling Shopify revenue to a locked anchor?
12. **No `SELECT *` on `dim_customer` / `bdg_customer_identity`** — five identifier columns are policy-tagged and unreadable, so a star select 403s with a message that looks like a broken connection. Name your columns; `customer_id` is what the analysis needs anyway (Rule 12).

---

## Running the query

Connection/setup lives in `references/setup_guide.md` — **Part A for Claude Code, Part B for a Mac terminal.** They authenticate differently and there is no `bq` CLI in Code.

**In Claude Code** the service account already reaches all 12 datasets, so there is no compose-here-run-there step: go from the question to `client.query(sql).result()` in one move, then straight into whatever the deliverable is (Excel via the `xlsx` skill, a deck via `pptx`, a hosted page via an artifact). Writing a `.sql` file first is only worth it when the query is something to keep and re-run.

Easier access raises the stakes on step 3, not lowers them. A wrong number now arrives faster and with more polish on it.

**On a Mac terminal:**

```bash
bq query --nouse_legacy_sql --project_id=biom-reporting-s26 '
  <SQL>
'
```

- **Dry-run cost check** before anything on a large/unfamiliar table: add `--dry_run`. Prints bytes scanned, zero cost. If a query you expect to be small shows GBs, add a partition filter (`fct_orders` is partitioned on `DATE(order_created_at_utc)`).
- **`--format=json` silently truncates to 100 rows** unless you pass `--max_rows=<N>`. Always set it for anything beyond a quick count — a past "data gap" here was really a truncated result set.
- Prefer naming columns over `SELECT *` on wide tables (readability + bytes scanned).

---

## Assembly and framing

- **Reconcile first.** Before quoting any number externally, check it against reference Section 6 anchors. Note that anchors get re-locked as fixes land — re-verify currency.
- **State the caveats from step 0** in the answer, not buried. For correlation claims (e.g. "dispenser owners buy more categories"), name the confounds (tenure, total spend) rather than implying causation — a sophisticated investor will find them otherwise.
- **Deposit validated queries.** When a query has been run and its output sanity-checked against anchors/expectations, add it to `references/validated_queries.md` with the date, the question it answers, and any gotchas hit. This is how the skill compounds.

---

## Reference files

Two-file split: **judgment** (curated, partial coverage, authoritative) vs **structure** (complete, regenerable, drifts).

- **`references/database_reference.md`** — the judgment layer: Section 1 rules, core tables + keys, bridges, product taxonomy, non-certified traps, locked anchors, SCD2 columns, join-key cheat sheet, and Section 9 order-line classification + channel scoping (join key, keyless-line trap, non-product exclusions, `order_source` taxonomy). **Read Section 1 before every query; read Section 9 before any product/category/channel query.**
- **`references/schema_map.md`** — the structure layer: complete column-level inventory of all 41 `biom_canvas` objects (types, partition/cluster keys, and full view SELECT logic). Consult when you need exact column names/types or a view's derivation. Objects flagged 🆕 there exist but aren't yet annotated in `database_reference.md` — verify grain/quirks live before relying on them. **It drifts, and silently:** it sat at 36 objects while the warehouse had 41, so five objects — including `vw_order_line_sku_resolved`, which supersedes a hand-rolled recipe in Section 9 — were invisible to anyone reading only this skill. Regenerate with `python scripts/refresh_schema.py` (works in Code and on a laptop; `refresh_schema.sh` is the older `bq`-only version and does not run in Code).
- **`references/setup_guide.md`** — one-time connection: gcloud install, the two required logins, project set, verification, CLI gotchas.
- **`references/validated_queries.md`** — a growing library of queries already run and checked. Check here first before writing a new query from scratch — the answer may already exist. Deposit each validated query here.
