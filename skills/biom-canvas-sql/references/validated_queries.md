# Validated Query Library — Biom CANVAS

A running log of queries that have been **run against live data and sanity-checked**. Check here before writing a new query from scratch. Each entry records the question, the query, the date, and any gotchas.

Status legend:
- ✅ **Validated** — run, output reconciled against anchors/expectations.
- 🟡 **Draft (unvalidated)** — written to spec but not yet run; flagged items must be confirmed against live data before trusting output.

---

## 🟡 Cross-sell across categories + dispenser-as-catalyst (VMG)

**Question:** As Biom adds categories, are customers buying into more than one? And do customers who buy dispensers buy into more distinct categories (the "dispenser as cross-sell catalyst" thesis)?

**Status:** 🟡 IN PROGRESS — do not quote. Join key confirmed (`CAST(variant_id AS STRING) = product_key`, ~94% match on non-NULL variants) and taxonomy spelling confirmed. **BLOCKER:** the draft below classifies via the `dim_product` join only, which silently drops the ~54K `variant_id IS NULL` lines — and those include active Starter Kits and Dispensers (see `database_reference.md` §9.2). The dispenser-catalyst cut in particular is invalid until dispensers are recovered from the keyless lines. Needs the two-tier resolver (taxonomy join + `product_title` fallback) and the §9.4 D2C `order_source` filter before it produces a defensible number. Earlier run (`avg 1.23 / 24.1% multi`) is an artifact of the dropped lines — discard.

**Category mapping** (uses the maintained `dim_product` taxonomy, per Rule 3 / Section 4 — not SKU regex):
- Cleaning (AP + DIS) = `product_sub_category IN ('APC','DSN')`
- Baby = `'Baby'` · Hand Sanitizing = `'Sanitizing'` · Body = `'Body Care'` · Flushable = `'Flushable'`
- Dispenser = `product_category = 'Dispensers'` → **format, not a category**; excluded from the category count.

```sql
WITH lines AS (
  SELECT
    o.customer_id,
    o.order_created_datetime_ct,
    CASE
      WHEN p.product_sub_category IN ('APC','DSN') THEN 'Cleaning'
      WHEN p.product_sub_category = 'Baby'         THEN 'Baby'
      WHEN p.product_sub_category = 'Sanitizing'   THEN 'Hand Sanitizing'
      WHEN p.product_sub_category = 'Body Care'    THEN 'Body'
      WHEN p.product_sub_category = 'Flushable'    THEN 'Flushable'
      WHEN p.product_category    = 'Dispensers'    THEN 'Dispenser'
      ELSE 'Other/Bundle'
    END AS investor_category
  FROM biom_canvas.fct_orders o
  JOIN biom_canvas.dim_product p
    -- FLAG 1 (confirm): product_key = shopify_variant_id, so this cast is the likely join.
    -- Verify vs the reference (product_key is STRING; variant_id is INT64).
    ON CAST(o.variant_id AS STRING) = p.product_key
  WHERE o.is_current = TRUE
    AND p.is_current = TRUE
    AND o.customer_id IS NOT NULL
    -- FLAG 2 (confirm): contaminant filters. Strip ShopMy $0 gifting + $1 PR/sample orders.
    -- Confirm the right column (a source/channel field, or an order-total threshold).
),
per_customer AS (
  SELECT
    customer_id,
    COUNT(DISTINCT IF(investor_category NOT IN ('Dispenser','Other/Bundle'),
                      investor_category, NULL)) AS n_categories,
    -- FLAG 3 (confirm): dispensers as UNITS needs a quantity column on fct_orders.
    -- If none exists, this counts dispenser LINES as a proxy — note the distinction.
    COUNTIF(investor_category = 'Dispenser') AS dispenser_lines
  FROM lines
  GROUP BY customer_id
)

-- A) Cross-sell headline
SELECT
  COUNT(*)                                     AS customers,
  ROUND(AVG(n_categories), 2)                  AS avg_categories,
  ROUND(AVG(IF(n_categories >= 2, 1.0, 0)), 3) AS pct_2plus_categories
FROM per_customer
WHERE n_categories >= 1;
```

Swap the final SELECT for the catalyst cut:

```sql
-- B) Dispenser as cross-sell catalyst
SELECT
  CASE WHEN dispenser_lines = 0 THEN '0 dispensers'
       WHEN dispenser_lines = 1 THEN '1 dispenser'
       ELSE '2+ dispensers' END               AS dispenser_cohort,
  COUNT(*)                                     AS customers,
  ROUND(AVG(n_categories), 2)                  AS avg_categories,
  ROUND(AVG(IF(n_categories >= 2, 1.0, 0)), 3) AS pct_multi_category
FROM per_customer
GROUP BY dispenser_cohort
ORDER BY dispenser_cohort;
```

**Gotchas / framing:**
- **Confound (must state to VMG):** the dispenser-cohort gap is correlation with tenure and total spend baked in — dispenser buyers are earlier/heavier customers. To neutralize, add `COUNT(DISTINCT order_id)` per customer as a control and show the category lift holds *within* order-count bands.
- **Cohort trend:** for "categories per customer is rising," hold the customer set fixed (same-cohort carry-forward). Baby (~Mar 2026) and Body (~May 2026) launching will mechanically lift an all-customer number.
- Guests are excluded (no `customer_id`). Consider `bdg_customer_identity` → `identity_key` if cross-channel identity matters.

---

## Warehouse inventory — what exists, and how stale is the schema map?
**Validated:** 2026-08-17 · **Answers:** "what datasets/objects can I actually reach, and does the skill still describe them?"

Run this before trusting `schema_map.md`. It caught the map sitting at 36 objects while `biom_canvas` held 41.

```sql
SELECT table_schema, COUNT(*) AS objects, COUNTIF(table_type = 'VIEW') AS views
FROM `biom-reporting-s26.region-us-central1.INFORMATION_SCHEMA.TABLES`
GROUP BY 1 ORDER BY objects DESC
```

**Gotchas:**
- **The region qualifier is mandatory.** `` `biom-reporting-s26`.INFORMATION_SCHEMA.TABLES `` returns **zero rows with no error** — the default US multi-region has none of these datasets. Same for the Python client: pass `location="us-central1"`.
- Expect 12 datasets. `biom_monitoring` (1,767 objects) is pipeline telemetry, not business data.
- To find undocumented objects, diff the live `table_name` set against the text of `schema_map.md` — that is how the five missing objects surfaced.

---

## Repurchase distribution — orders per customer, PII-free
**Validated:** 2026-08-17 · **Answers:** "what share of customers ever order more than once?"

Demonstrates that customer analysis needs no identifier columns (Rule 12): `customer_id` alone carries it.

```sql
WITH per_cust AS (
  SELECT customer_id, COUNT(DISTINCT order_id) AS orders
  FROM `biom-reporting-s26.biom_canvas.fct_orders`
  WHERE is_current AND customer_id IS NOT NULL
  GROUP BY 1
)
SELECT IF(orders = 1, '1 order', IF(orders <= 3, '2-3 orders', '4+ orders')) AS band,
       COUNT(*) AS customers
FROM per_cust GROUP BY 1 ORDER BY 1
```

**Result (2026-08-17):** 1 order 25,893 (51.9%) · 2–3 orders 13,029 (26.1%) · 4+ orders 11,016 (22.1%) · total 49,938.

**Gotchas:**
- This is **all channels and no contaminant filter** — a raw shape, not a reportable repurchase rate. For anything external, scope `order_source` to core D2C (§9.4) and strip ShopMy $0 gifting and $1 PR samples first, or the denominator is inflated by orders that were never purchases.
- 49,938 customers here vs 142,417 `customer_id`s in `dim_customer`: most customer records have no attributable order line. Do not treat `dim_customer` row count as a customer base.
- `vw_repurchase_base` already models this properly (first/second order dates, 60-day windows, acquisition channel). Prefer it for real analysis; the above is a sanity shape.

---

## Keyless order lines — how much revenue resolves to no variant?
**Validated:** 2026-08-17 · **Answers:** "how complete is any variant-keyed product total?" (§9.2)

```sql
SELECT resolution_method, COUNT(*) AS lines,
       COUNTIF(resolved_variant_id IS NULL) AS unresolved,
       ROUND(SUM(gross_using_line_price), 0) AS gross
FROM `biom-reporting-s26.biom_canvas.vw_order_line_sku_resolved`
GROUP BY 1 ORDER BY lines DESC
```

**Result (2026-08-17):** 233,440 lines. `NO_PATH` = 25,570 lines / **$1.05M gross that resolves to no variant.** Of 53,999 keyless lines the view rescues 26,595 (49%).

**Gotchas:**
- Run this before quoting any variant-keyed product/category total — it tells you what the total is missing. Roughly $1.05M is unattributable, so a resolved figure is a floor, not the whole.
- `NON_PRODUCT` (2,067 lines) is the §9.3 exclusion set, pre-labelled.
