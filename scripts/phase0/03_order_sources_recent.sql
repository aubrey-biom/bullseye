-- Phase 0: is the order_source taxonomy still current? Anything new here needs a
-- row in bq.DTC_SOURCE_BUCKETS before the D2C scope filter is trusted.
-- Run 2026-09-10 and resolved against Shopify admin: 242196283393 = ShopMy
-- Integration (gifting, $0 orders at list value), 3890849 = Shop app (real D2C),
-- Direct = Matrixify imports ($0 reward claims), NULL = 2026-06-18..29 load gap.
SELECT order_source, COUNT(DISTINCT order_id) AS orders,
       ROUND(SUM(gross_using_line_price), 2) AS list_value_gross,
       ROUND(SUM(net_line_sales), 2) AS net_line,
       COUNT(DISTINCT IF(current_total_price > 1, order_id, NULL)) AS paid_orders,
       MIN(DATE(order_created_datetime_ct)) AS first_day,
       MAX(DATE(order_created_datetime_ct)) AS last_day
FROM `biom-reporting-s26.biom_canvas.fct_orders`
WHERE is_current
  AND DATE(order_created_datetime_ct) >= DATE_SUB(CURRENT_DATE('America/Chicago'), INTERVAL 90 DAY)
GROUP BY order_source
ORDER BY orders DESC
