# BIOM CANVAS — Database Reference
**Project:** `biom-reporting-s26` · **Region:** `us-central1` · **Primary dataset:** `biom_canvas`
**Purpose:** Everything you need to understand the database and write correct queries. This is a curated extract from BIOM's internal engineering docs — database/schema content only, no pipeline/deployment/operational material.
**Read this before writing any query.** Almost every mistake on this platform's history traces back to skipping one of the rules in Section 1.

---

## 0. Mental Model — How the Data Flows

```
biom_raw  →  biom_core  →  biom_marts  →  biom_canvas
(raw ingest) (deduped,     (intermediate)  (THIS is what
              typed)                        you query)
```

**You almost never need to touch `biom_raw`, `biom_core`, or `biom_marts` directly.** `biom_canvas` is the certified, analyst-facing analytical layer — every table there has already been deduplicated, typed, and validated. If you're writing a business question query, it should almost always be against `biom_canvas`.

Six source systems feed this warehouse: **Shopify** (DTC storefront), **Loop** (subscriptions), **Malomo** (shipment tracking), **Target/BPD** (primary retail revenue channel — bigger than Shopify), **Google Ads**, **Meta Ads**.

### The other datasets are readable. That is not an invitation.

Twelve datasets are visible, not four. `biom_canvas` (41 objects) is still where answers come from — the rest are readable so you can *debug lineage* when a canvas number looks wrong, and for nothing else. If you find yourself composing a business answer out of `biom_raw`, stop: either the canvas table you want exists and you missed it, or there is a modelling gap worth raising rather than routing around.

| dataset | objects | what it's for |
|---|---|---|
| `biom_canvas` | 41 | **the answer layer** — query this |
| `biom_raw` / `biom_core` / `biom_marts` | 33 / 14 / 5 | lineage debugging only |
| `bpd_raw` / `bpd_curated` / `bpd_meta` | 18 / 5 / 1 | Target staging; `bpd_curated` is deprecated (Rule 10, Section 5) |
| `biom_identity` | 6 | identity resolution inputs |
| `biom_admin` | 13 | warehouse admin |
| `meta_raw` | 4 | Meta Ads staging |
| `biom_google_ads_raw` | 190 | Google Ads staging — 95 views, easy to get lost |
| `biom_monitoring` | 1,767 | pipeline run telemetry. **Not business data.** Do not browse. |

---

## 1. THE RULES — Read This Section First, Every Time

### Rule 1 — Always filter SCD2 tables to current version
```sql
WHERE is_current = TRUE
```
Most `biom_canvas` tables use SCD Type 2 (slowly changing dimension) history tracking — every change to a row creates a new version rather than overwriting. Omitting this filter returns **every historical version** and silently inflates every count and sum.

**Exception:** Views (`vw_revenue_subscriptions`, `vw_target_week_store`, `vw_target_week_tcin`) already apply this filter internally. Adding `is_current = TRUE` again on a view **double-filters and produces wrong results** — only add it when querying base tables directly.

### Rule 2 — Never SUM order-level money columns at line grain
`fct_orders` is **line-item grain** — one row per order line — but several columns are order-level totals that repeat identically on every line of a multi-line order.
```sql
-- WRONG — inflates the total by however many lines the order has
SELECT SUM(current_total_price) FROM fct_orders WHERE order_id = 'X'

-- RIGHT — these columns are pre-computed to be SUM-safe at line grain
SELECT SUM(gross_using_line_price) FROM fct_orders WHERE order_id = 'X'
```
**Never SUM these:** `current_total_price`, `current_subtotal_price`, `current_total_discounts`, `total_shipping`, `total_tax`.
**Safe to SUM:** `gross_using_line_price`, `net_line_sales`.

### Rule 3 — `variant_id` is the ONLY valid product join key. SKU is NEVER a join key.
SKU is a mutable display label, not unique. Multiple product variants can share one SKU (a known pattern here, nicknamed `M4 DUPLICATE_SKU`).
```sql
-- WRONG
JOIN dim_product_variant ON fct_orders.sku = dim_product_variant.sku
-- RIGHT
JOIN dim_product_variant ON fct_orders.variant_id = dim_product_variant.variant_id
```
For Target products, the equivalent join key is `tcin` (see Rule 8 for a type gotcha).

### Rule 4 — `fct_refunds` is NOT automatically applied anywhere else
`net_revenue_amount` in `fct_revenue` is post-discount only — **refunds are never subtracted** at the row level.
```sql
-- True net revenue, refunds included:
SELECT
  r.order_id,
  SUM(r.net_revenue_amount) - COALESCE(SUM(ref.total_refunded), 0) AS true_net
FROM fct_revenue r
LEFT JOIN fct_refunds ref
  ON CAST(ref.order_id AS STRING) = r.order_id
  AND ref.is_current = TRUE
WHERE r.is_current = TRUE AND r.channel_key = 'shopify'
GROUP BY r.order_id
```

### Rule 5 — The subscription status column is named `status`, not `subscription_status`
```sql
-- fct_subscriptions:
WHERE status = 'ACTIVE'
-- NOT: WHERE subscription_status = 'ACTIVE'  ← will error, column doesn't exist here
```
The friendlier name `subscription_status` only exists as a view-level alias inside `vw_revenue_subscriptions` (and as a genuine renamed column inside the `bdg_order_subscription` bridge table specifically — the one exception).

### Rule 6 — Target money is FLOAT64; Shopify money is NUMERIC
Handle rounding carefully when combining the two — `ROUND()` before display to avoid floating-point artifacts on Target figures.

### Rule 7 — `fct_refunds.order_id` type mismatch
```sql
-- fct_refunds.order_id is INT64; fct_orders.order_id is STRING
CAST(fct_refunds.order_id AS STRING) = fct_orders.order_id
```

### Rule 8 — `dim_product.tcin` is STRING; `fct_revenue.tcin` is INT64
```sql
SAFE_CAST(dim_product.tcin AS INT64) = fct_revenue.tcin
```
Always use `SAFE_CAST`, not `CAST` — a small number of TCINs don't cleanly round-trip.

### Rule 9 — NEVER use `net_revenue_amount` for a "net" KPI — always use `admin_net_revenue`
`net_revenue_amount` is unreliable (see Rule 4). The correct, admin-reconciled net figure is `admin_net_revenue`, and it **only exists inside the view `vw_revenue_subscriptions`** — it is not a column on `fct_revenue` or `fct_orders` at all.
```sql
-- WRONG — this column does not exist on fct_revenue, query will error
SELECT SUM(admin_net_revenue) FROM biom_canvas.fct_revenue WHERE is_current = TRUE

-- CORRECT
SELECT SUM(admin_net_revenue) FROM biom_canvas.vw_revenue_subscriptions
```

### Rule 10 — Target data grain-priority
Never use `bpd_curated.sales_all` (grain-overlap issues, deprecated) or any `bpd_curated.*` view for reporting (see Section 5). Use `fct_target_sales`, which already has grain-priority cutoffs applied: `daily >= 2026-05-06`, `weekly 2026-04-04 to 2026-05-05`, `history_weekly < 2026-04-04`.

### Rule 11 — Use Central Time for Shopify revenue reconciliation
All locked Shopify revenue anchors (Section 6) use America/Chicago. `fct_orders.order_created_datetime_ct` is pre-converted — use it, not the UTC column, when comparing to a locked anchor figure.

### Rule 12 — Customer identifiers are locked. `SELECT *` fails on two tables.
Five columns sit behind the `biom-pii / direct-identifier` policy tag and are unreadable without the Fine-Grained Reader role, which the Claude Code service account deliberately does not hold:

| table | columns |
|---|---|
| `dim_customer` | `email`, `first_name`, `last_name`, `phone` |
| `bdg_customer_identity` | `normalized_email` |

```sql
-- FAILS: 403 "User has neither fine-grained reader nor masked reader"
SELECT * FROM `biom-reporting-s26.biom_canvas.dim_customer` WHERE is_current
-- WORKS: name the columns you need
SELECT customer_id, created_at FROM `biom-reporting-s26.biom_canvas.dim_customer` WHERE is_current
```

That error reads like a broken connection or a missing dataset grant. It is neither — it means a tagged column was selected, usually by a `SELECT *` nobody thought about.

**This costs you nothing analytically.** `customer_id` is the join key for every cohort, repurchase, LTV, and cross-sell question in this warehouse; `vw_repurchase_base` is built entirely on it. No analysis here needs a name, an email, or a phone number. If you genuinely need to reach a specific customer, that lookup belongs in Shopify admin, not in a SQL result set.

Applies to whoever runs the query, not to the warehouse: a human with `bigquery.admin` **plus** Fine-Grained Reader sees these columns normally.

---

## 2. Core Tables You'll Actually Use

### `dim_product` — Product catalog (Shopify + Target combined)
- **Join key:** `product_key` (STRING) = `shopify_variant_id`, or `'TCIN-' + tcin` for Target-exclusive products
- **Taxonomy columns:** `product_category` (Dispensers / Cleaning / Personal Care / Other) and `product_sub_category` (11 values — Full Size, Mini, Baby, Holiday & Seasonal, Bundles & Kits, APC, DSN, Flushable, Sanitizing, Body Care, Accessories, Unassigned)
- Category/sub-category are populated by a locked, priority-ordered SKU-pattern rule set — see Section 4.
- **Known valid pairings (updated 2026-07-21):** `Holiday & Seasonal` and `Accessories` only ever pair with `category=Other`. `Baby` only pairs with `Personal Care`. `Bundles & Kits` can pair with `Cleaning`, `Personal Care`, or `Other` depending on the kit's identifiable wipe type.
- **⚠️ Two non-product SKU classes are excluded from this table entirely:** Redo's return-protection add-on (`sku='x-redo'`) and catalog test placeholders (`sku IN ('1111','2222')`) — never real products.

### `dim_product_variant` — Shopify variant-level detail
- **Join key:** `variant_id` (INT64) — the immutable Shopify `legacyResourceId`. This is the correct join key for anything variant-level (see Rule 3).
- `is_order_active` (BOOL) disambiguates the `M4 DUPLICATE_SKU` pattern (two active variants sharing one SKU) — filter to `TRUE` when you need "the real current one."

### `dim_customer` — Shopify customers
- **Join key:** `customer_id` (INT64)
- `has_loop` (BOOL) — TRUE if this customer has a Loop subscription
- `currency` is NULL on ~53,103 bulk-backfilled rows — a known, accepted gap (REST API bulk export had no currency field equivalent)

### `fct_orders` — Shopify orders, line-item grain
- **Natural key:** composite `(order_id, line_item_id)`, both STRING
- **Grain: one row per order LINE**, but order-header columns (customer, dates, status, totals) repeat on every line — see Rule 2 before summing anything
- `purchase_type` — `'Subscription'` / `'One Time'` / NULL. This is the platform's **one certified, locked** subscription classifier at the order level (see Section 4). NULL occurs on rows sourced from `shopify_orders_current` (a schema gap, not a real ambiguity) and on ~5,905 pre-June-19 historical rows (unresolved, tracked as a backlog item).
- `variant_id` is NULL on ~53,987 historical/custom lines (deleted variants, kit line items) — expected, not a bug.
- Partitioned on `DATE(order_created_at_utc)`.

### `fct_revenue` — Unified Shopify + Target revenue
- **Natural key:** `revenue_key` — channel-prefixed composite (`SHOP:order_id:line_item_id` or `TGT:date:tcin:location:...`)
- `channel_key` = `'shopify'` or `'target'` — this indicates data **source**, not storefront name (Loop subscription revenue lives inside `'shopify'` rows, filtered via `purchase_type = 'Subscription'`)
- `revenue_amount` = gross; `net_revenue_amount` = post-discount only, **refunds not subtracted** (Rule 4)

### `fct_refunds` — Shopify refunds
- **Grain:** one row per refund header (not per refund line item)
- `order_id` is INT64 here vs STRING on `fct_orders` — cast when joining (Rule 7)

### `fct_subscriptions` — Loop subscriptions
- **Natural key:** `subscription_id` (STRING)
- Column is named `status`, not `subscription_status` (Rule 5)
- `origin_order_shopify_id` is NULL for 6,895 pre-Recharge-migration subscriptions — expected, tracked, not a data quality issue
- 11.4% of subscriptions have 2+ product lines; `variant_id` on this table is the **primary line only**

### `fct_target_sales` / `fct_target_inventory` — Target/BPD retail
- Grain-priority cutoffs already applied (Rule 10) — always use these tables, never raw `bpd_raw.*` or `bpd_curated.*` for reporting
- Target is the **primary revenue channel** — larger than Shopify DTC

### `fct_delivery` — Malomo shipment tracking
- Known limitation: Amazon Logistics shipments are permanently unsupported by Malomo (`unsupported_carrier_error`) — not a bug, a Malomo platform limitation
- **Separate, important context:** BIOM migrated actual customer-facing tracking from Malomo to **Redo** on 2026-06-12. This table still only reflects Malomo's data — it is not fully up to date with real tracking status since that migration. A fix is in progress; treat delivery-status freshness with caution until that's resolved.

### `vw_revenue_subscriptions` — The view you want for net revenue
- Combines Shopify + Target revenue rows with Loop subscription status in one UNION ALL
- **This is the only place `admin_net_revenue` exists** (Rule 9)
- Already filters `is_current = TRUE` internally — don't re-add it (Rule 1 exception)
- Known small artifact: a `products_by_sku` fallback join creates a ~$35 (0.018% at May 2026 volume) gross-revenue overstatement — tracked, non-material today, scales with volume

### `vw_repurchase_base` — Customer repurchase analytics (new, 2026-07-20)
- **Grain:** one row per customer (Shopify customers with ≥1 order; guests excluded)
- Built to answer repurchase-rate questions without re-deriving order sequencing yourself
- Key columns: `is_repurchaser_2plus` (any 2nd order, ever), `is_repurchaser_within_60d` (2nd order within 60 days — a locked assumption, not data-derived), `is_repurchaser_one_time_2plus` (isolates genuine discretionary reorders, excluding subscription renewals), `subscription_segment`, `first_purchase_category`
- **Important:** don't compare `subscription_segment` repurchase rates directly against `one_time` segment rates as if equivalent — a subscription renewal counts as a "repurchase" here, which mechanically inflates that segment's rate. Use `is_repurchaser_one_time_2plus` for an apples-to-apples comparison.
- `first_purchase_category = 'Unknown'` remains on a small residual population (~5%), concentrated in the store's earliest history (2021-2022) — not an ongoing gap.

---

## 3. Bridges (for cross-table/cross-channel lookups)

- **`bdg_product_channel`** — links a `product_key` to which channel(s) (Shopify/Target) it's sold in
- **`bdg_order_subscription`** — links a Shopify `order_id` to the Loop `subscription_id` it originated
- **`bdg_customer_identity`** — links a Shopify `customer_id` to a cross-channel `identity_key` (note: 24 identity_keys are shared by 2 merged Shopify accounts each)

---

## 4. Product Taxonomy — How Categorization Actually Works

Every product's `product_category`/`product_sub_category` is assigned by a **locked, priority-ordered SKU pattern match** (first match wins), roughly:

1. `P-DIS-%` / `CSP-DIS-%` → Dispensers / Full Size
2. `P-6DIS-%` / `CSP-6DIS-%` → Dispensers / Mini
3. `K-HOL-%` or title contains "Holiday" → Other / Holiday & Seasonal
4. Various `K-%DIS-%` kit patterns → Other / Bundles & Kits (reclassified as of 2026-07-21 to split into Cleaning/Personal Care/Other by identifiable wipe type where possible)
5. `%WIP-AP-%` → Cleaning / APC
6. `%WIP-DSN-%` → Cleaning / DSN
7. `%WIP-BAB-%` or title has "Baby" → Personal Care / Baby
8. `%WIP-FLU-%` → Personal Care / Flushable
9. `%WIP-SAN-%` / `%WIP-SA-%` → Personal Care / Sanitizing
10. `%WIP-BOD-%` → Personal Care / Body Care
11. `P-SWG-%` → Other / Accessories
12. Anything else → Other / Unassigned

**⚠️ Unresolved discrepancy in the source docs (as of this writing) — verify live before relying on either claim:**
Two specific SKU-pattern fixes were reported as real, verified findings on 2026-07-20 (`P-UDIS-%` recovering 62 customers; `K-WIP-MIX-SKU-TRV-3PK` affecting 2 customers) — but a later dated correction in the same source documentation states **neither SKU pattern exists anywhere in live data**, and that both original claims were never actually verified. This has not yet been resolved. **Do not treat either claim as fact** until confirmed with a live query:
```sql
SELECT COUNT(*) FROM biom_canvas.dim_product_variant WHERE sku LIKE 'P-UDIS-%';
SELECT COUNT(*) FROM biom_canvas.fct_orders WHERE sku = 'K-WIP-MIX-SKU-TRV-3PK';
```

A related, separate, **confirmed-real** issue: `dim_product`'s taxonomy briefly regressed to 83 mis-categorized ("Unassigned") products (found 2026-07-20, root-caused and fixed 2026-07-22 — commit `7c978ec`, 0 Unassigned remaining as of that fix).

---

## 5. Things That Look Like Data But Aren't Certified — Avoid These

**`bpd_curated.*` views** (`gross_margin_all`, `inventory_all`, `inventory_latest`, `items`, `locations`) — these exist and are queryable, but are **not part of the certified CANVAS layer**. They lack the grain-priority/dedup fixes already applied to their `biom_canvas` equivalents. Use `fct_target_sales`/`fct_target_inventory`/`dim_location` instead for anything reported externally.

---

## 6. Locked Revenue Anchors (May 2026)

If a query produces a different number than these, **the query is wrong, not the anchor.** Always re-verify these are still current before quoting them externally — anchors get re-locked periodically as fixes land.

| Channel | Metric | Value | Basis |
|---|---|---|---|
| Shopify | Gross revenue | $313,713.87 | `gross_using_line_price`, Central Time |
| Shopify | Orders | 5,377 | — |
| Shopify | Units sold | 10,647 | — |
| Shopify | Admin net revenue | $208,190.31 | `admin_net_revenue`, order-date basis, as-of 2026-07-09 |
| Target | Net revenue | $1,528,941.67 | `sale_amount`, grain-priority cutoffs |
| Target | TCINs / Locations | 30 / 1,907 | — |

**Target's earlier $3.15M figure (seen in old documents) was a triple-count error** — daily + weekly + history_weekly grains stacked without dedup. $1,528,941.67 is the correct, current figure (~4.9× Shopify, not 10×).

---

## 7. SCD2 System Columns (present on most tables)

| Column | Meaning |
|---|---|
| `is_current` | TRUE = latest version. **Always filter to this on base tables (Rule 1).** |
| `valid_from` / `valid_to` | Version validity window. NULL `valid_to` = still active. |
| `is_deleted` | TRUE if the record disappeared from the upstream source |
| `record_hash` | Change-detection hash over business columns |
| `created_at` / `updated_at` | First-seen / this-version-written timestamps |

---

## 8. Analytical Spine Cheat Sheet

| Domain | Correct join key | Never use |
|---|---|---|
| Shopify products | `variant_id` (INT64) | `sku` |
| Target products | `tcin` (SAFE_CAST types differ by table) | `manufacturer_style` alone |
| Customers | `customer_id` (INT64), or `identity_key` cross-channel | — |
| Subscriptions | `subscription_id` (STRING) | — |

---

## 9. Order-Line Classification & Channel Scoping (validated live 2026-07-22)

Findings confirmed by live query while building the SQL skill. General to any product/category/SKU/channel analysis on `fct_orders`.

### 9.1 — Confirmed `fct_orders` → `dim_product` join
```sql
JOIN dim_product p ON CAST(fct_orders.variant_id AS STRING) = p.product_key AND p.is_current = TRUE
```
`product_key` is the bare Shopify variant id stored as STRING (e.g. `40824883904678`); `fct_orders.variant_id` is INT64 — hence the cast. **Match rate: of lines with non-NULL `variant_id`, ~94% join (5.79% unmatched — a small orphan set, acceptable).** `dim_product_variant` carries **no category columns**, so it is not a required hop for taxonomy — join `fct_orders` straight to `dim_product`.

### 9.2 — The keyless-line trap (CRITICAL for any product/category analysis)
~53,995 `fct_orders` lines have `variant_id IS NULL` (also `product_id IS NULL`) — the known ~54K NULL-variant population (Section 6 of the tables notes). **These are NOT all historical junk — the highest-volume among them are ACTIVE flagship products,** classifiable only by `product_title`:
- Starter Kits — e.g. `All-Purpose Cleaning Wipes Starter Kit` = 15,480 lines / ~$720K gross (plus many other kit titles)
- Dispensers — `Dispenser` (10,156) + `Dispenser - Limited Offer` (3,909) + `Biom Home™ Dispenser 2.0` (1,516) ≈ 15.5K dispenser lines
- Refill limited offers, travel packs, holiday variants

An analysis keyed **only** on the `dim_product` join silently drops all of these — massively undercounting dispensers and kits. **Rule: for product/category/dispenser analysis, use a two-tier resolver** — authoritative `dim_product` taxonomy when the join succeeds; a `product_title` pattern fallback when `variant_id IS NULL`.

**Prefer `vw_order_line_sku_resolved` over hand-rolling that resolver** (added since this section was written; verified live 2026-08-17). It carries the resolution already done, plus a `resolution_method` column saying *how* each line was resolved, so coverage is measurable instead of assumed. 233,440 lines:

| `resolution_method` | lines | unresolved | gross |
|---|---|---|---|
| `DIRECT_VARIANT_ID` | 163,035 | 0 | $5.12M |
| `NO_PATH` | 25,570 | **25,570** | **$1.05M** |
| `SKU_HISTORY_FALLBACK` | 17,276 | 0 | $573K |
| `DEAD_VARIANT` | 16,193 | 0 | $572K |
| `SKU_HISTORY_DATED` | 8,674 | 0 | $217K |
| `NON_PRODUCT` | 2,067 | 2,067 | $2.8K |
| `SKU_HISTORY_BACKDATED` / `_NULLSKU` / `AMBIGUOUS` | 625 | 1 | $18K |

**It narrows the problem, it does not erase it.** Of the 53,999 keyless lines it rescues 26,595 — 49%. `NO_PATH` still holds 25,570 lines and **$1.05M of gross that resolves to no variant at all**. Any total built off `resolved_variant_id` is therefore short by roughly that much: state it, or reconcile against an unresolved total, but do not present the resolved figure as complete. `NON_PRODUCT` is the §9.3 exclusion set, already labelled for you.

### 9.3 — Non-product line items to exclude
Present as `fct_orders` lines but not sellable products (≈$0 or trivial gross): titles `Carbon Neutral Offset`, `Checkout+`, `20% CashBack` (and any `%CashBack%`), and gift-with-purchase `%GWP%` (e.g. `Hydrangea Dispenser GWP`). Filter these out of product/revenue-mix work.

### 9.4 — `order_source` taxonomy (D2C vs wholesale/marketplace)
- **Core D2C:** `web`, `subscription_contract`, `subscription_contract_checkout_one`
- **App-sourced, incl. ShopMy gifting:** numeric app-IDs (`242196283393`, `2329312`, `3890849`, `1424624`, …)
- **Wholesale / marketplace / POS — exclude from D2C analyses:** `faire`/`Faire`, `Design Milk Shop`, `pos`, `BetterWorld`, `Flora`, `Sustai Market`, `Canal`, `Choose`, `shopify-collective-automatic-payments`, `shopify_draft_order`
- Default a "D2C customer" definition to the three core-D2C sources unless the question says otherwise. (`current_total_price > 1.00` additionally strips $0 gifting / $1 PR samples but does not catch non-zero marketplace orders — combine with the source filter.)

---

## 10. Complete Structural Map

For the full column-level inventory of all 36 `biom_canvas` objects (every table, column, type, partition/cluster key, and view SELECT logic), see the companion **`schema_map.md`**. That file is regenerable structure; this file is curated judgment. Tables flagged 🆕 in `schema_map.md` (ad/Meta/keyword/shopping performance, `dim_date`, `dim_channel`, `dim_subscription_plan`, `fct_inventory`, `fct_subscription_events`, `fct_target_gross_margin`, and views `vw_shopify_sku_order_financial_detail`, `vw_variant_sku_journey`, `vw_shopify_category_geo_detail`, `vw_target_week_store`, `vw_target_week_tcin`) exist in the warehouse but are **not yet annotated with grain/quirks here** — verify their behavior with a live query before relying on them, then capture what you learn back into this file.

---

*This is a curated extract, not the full internal documentation. For anything not covered here — pipeline schedules, deployment, monitoring — that's operational detail this reference deliberately excludes, since it's not needed for querying the database.*
