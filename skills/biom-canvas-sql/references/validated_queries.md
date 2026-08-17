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
