-- Phase 0 / DTC + ads enhancement: do the four ad facts have data, how far does it
-- reach, and are the value columns (needed for platform ROAS) actually populated?
-- Run 2026-09-10: all four current through 2026-09-09, loaded that morning; Meta
-- history starts 2025-07-02; keyword + shopping spend is ~40% of campaign spend.
SELECT 'meta_performance' AS fact, MIN(date_start) AS first_day, MAX(date_start) AS last_day,
       COUNT(*) AS row_count, COUNT(DISTINCT date_start) AS days,
       ROUND(SUM(spend), 2) AS spend, SUM(clicks) AS clicks,
       COUNTIF(purchase_value IS NOT NULL AND purchase_value > 0) AS rows_with_value,
       MAX(loaded_at) AS last_loaded_at
FROM `biom-reporting-s26.biom_canvas.fct_meta_performance`
UNION ALL
SELECT 'google_ad_performance', MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT date),
       ROUND(SUM(spend_usd), 2), SUM(clicks),
       COUNTIF(conversions_value IS NOT NULL AND conversions_value > 0), MAX(loaded_at)
FROM `biom-reporting-s26.biom_canvas.fct_ad_performance`
UNION ALL
SELECT 'google_shopping_performance', MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT date),
       ROUND(SUM(spend_usd), 2), SUM(clicks),
       COUNTIF(conversions_value IS NOT NULL AND conversions_value > 0), MAX(loaded_at)
FROM `biom-reporting-s26.biom_canvas.fct_shopping_performance`
UNION ALL
SELECT 'google_keyword_performance', MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT date),
       ROUND(SUM(spend_usd), 2), SUM(clicks),
       COUNTIF(conversions_value IS NOT NULL AND conversions_value > 0), MAX(loaded_at)
FROM `biom-reporting-s26.biom_canvas.fct_keyword_performance`
ORDER BY fact
