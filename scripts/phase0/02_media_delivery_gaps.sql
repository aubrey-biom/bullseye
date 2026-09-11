-- Phase 0: how many ad-spend days are real zeros vs undiagnosed pipeline gaps, per channel.
-- A pacing tool must flag ABSENT_UNDIAGNOSED days, never zero-fill them.
-- Run 2026-09-10: zero ABSENT_UNDIAGNOSED days on either channel.
SELECT channel, delivery_status, COUNT(*) AS days,
       MIN(event_date) AS first_day, MAX(event_date) AS last_day,
       ROUND(SUM(spend_modelled), 2) AS spend
FROM `biom-reporting-s26.biom_canvas.vw_media_delivery_status`
GROUP BY channel, delivery_status
ORDER BY channel, days DESC
