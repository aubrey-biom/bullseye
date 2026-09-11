# Biom CANVAS — Structural Schema Map
**Project:** `biom-reporting-s26` · **Dataset:** `biom_canvas` · **Region:** `us-central1`
**Generated:** 2026-08-17 from `INFORMATION_SCHEMA` · **Objects:** 30 base tables, 11 views · **Columns:** 954

> **This file is STRUCTURE, regenerable on demand — not hand-curated truth.** It goes stale as the warehouse changes. Regenerate with `python scripts/refresh_schema.py`. For grain, join keys, quirks, SUM-safe columns, and the correctness rules, see **`database_reference.md`** — that is the judgment layer. Objects flagged 🆕 appear in the live warehouse but are **not yet annotated** in `database_reference.md`: their structure is below but their grain/quirks are unverified — confirm before relying on them.

## Contents

**Base tables:** `bdg_customer_identity`, `bdg_order_subscription`, `bdg_product_channel`, `dim_ad_group` 🆕, `dim_ad_meta` 🆕, `dim_adset_meta` 🆕, `dim_campaign` 🆕, `dim_campaign_meta` 🆕, `dim_channel`, `dim_customer`, `dim_date`, `dim_location`, `dim_product`, `dim_product_discontinued` 🆕, `dim_product_variant`, `dim_subscription_plan`, `fct_ad_performance` 🆕, `fct_delivery`, `fct_inventory`, `fct_keyword_performance` 🆕, `fct_meta_performance` 🆕, `fct_orders`, `fct_refunds`, `fct_revenue`, `fct_shopping_performance` 🆕, `fct_subscription_events`, `fct_subscriptions`, `fct_target_gross_margin`, `fct_target_inventory`, `fct_target_sales`

**Views:** `vw_inventory_oos` 🆕, `vw_media_delivery_status` 🆕, `vw_mmm_phase3_weekly_dataset` 🆕, `vw_order_line_sku_resolved` 🆕, `vw_repurchase_base`, `vw_revenue_subscriptions`, `vw_shopify_category_geo_detail`, `vw_shopify_sku_order_financial_detail`, `vw_target_week_store`, `vw_target_week_tcin`, `vw_variant_sku_journey`

---

## Base Tables

### `bdg_customer_identity`
**Clustered:** `is_current, has_loop`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `customer_id` | INT64 |  |
| 2 | `identity_key` | STRING |  |
| 3 | `normalized_email` | STRING | ✓ |
| 4 | `has_loop` | BOOL | ✓ |
| 5 | `row_id` | STRING |  |
| 6 | `valid_from` | TIMESTAMP |  |
| 7 | `valid_to` | TIMESTAMP | ✓ |
| 8 | `is_current` | BOOL |  |
| 9 | `is_deleted` | BOOL |  |
| 10 | `record_hash` | STRING |  |
| 11 | `created_at` | TIMESTAMP |  |
| 12 | `updated_at` | TIMESTAMP |  |

### `bdg_order_subscription`
**Clustered:** `is_current, subscription_status`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `order_id` | STRING |  |
| 2 | `subscription_id` | STRING |  |
| 3 | `subscription_status` | STRING | ✓ |
| 4 | `is_loop_subscription_certified` | BOOL | ✓ |
| 5 | `is_loop_revenue_certified` | BOOL | ✓ |
| 6 | `row_id` | STRING |  |
| 7 | `valid_from` | TIMESTAMP |  |
| 8 | `valid_to` | TIMESTAMP | ✓ |
| 9 | `is_current` | BOOL |  |
| 10 | `is_deleted` | BOOL |  |
| 11 | `record_hash` | STRING |  |
| 12 | `created_at` | TIMESTAMP |  |
| 13 | `updated_at` | TIMESTAMP |  |

### `bdg_product_channel`
**Clustered:** `is_current, channel_key`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `product_key` | STRING |  |
| 2 | `channel_key` | STRING |  |
| 3 | `sku` | STRING | ✓ |
| 4 | `shopify_variant_id` | STRING | ✓ |
| 5 | `tcin` | STRING | ✓ |
| 6 | `manufacturer_style` | STRING | ✓ |
| 7 | `is_in_shopify` | BOOL | ✓ |
| 8 | `is_in_target` | BOOL | ✓ |
| 9 | `row_id` | STRING |  |
| 10 | `valid_from` | TIMESTAMP |  |
| 11 | `valid_to` | TIMESTAMP | ✓ |
| 12 | `is_current` | BOOL |  |
| 13 | `is_deleted` | BOOL |  |
| 14 | `record_hash` | STRING |  |
| 15 | `created_at` | TIMESTAMP |  |
| 16 | `updated_at` | TIMESTAMP |  |

### `dim_ad_group` 🆕
**Clustered:** `is_current, ad_group_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `ad_group_id` | INT64 |  |
| 2 | `campaign_id` | INT64 | ✓ |
| 3 | `customer_id` | INT64 | ✓ |
| 4 | `ad_group_name` | STRING | ✓ |
| 5 | `ad_group_status` | STRING | ✓ |
| 6 | `ad_group_type` | STRING | ✓ |
| 7 | `ad_group_ad_rotation_mode` | STRING | ✓ |
| 8 | `ad_group_cpc_bid_micros` | INT64 | ✓ |
| 9 | `ad_group_effective_target_roas` | FLOAT64 | ✓ |
| 10 | `ad_group_effective_target_roas_source` | STRING | ✓ |
| 11 | `ad_group_effective_target_cpa_micros` | INT64 | ✓ |
| 12 | `ad_group_effective_target_cpa_source` | STRING | ✓ |
| 13 | `campaign_bidding_strategy_type` | STRING | ✓ |
| 14 | `row_id` | STRING |  |
| 15 | `valid_from` | TIMESTAMP |  |
| 16 | `valid_to` | TIMESTAMP | ✓ |
| 17 | `is_current` | BOOL |  |
| 18 | `is_deleted` | BOOL |  |
| 19 | `record_hash` | STRING |  |
| 20 | `created_at` | TIMESTAMP |  |
| 21 | `updated_at` | TIMESTAMP |  |

### `dim_ad_meta` 🆕
**Clustered:** `is_current, ad_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `ad_id` | STRING |  |
| 2 | `adset_id` | STRING | ✓ |
| 3 | `campaign_id` | STRING | ✓ |
| 4 | `name` | STRING | ✓ |
| 5 | `status` | STRING | ✓ |
| 6 | `meta_created_at` | TIMESTAMP | ✓ |
| 7 | `meta_updated_at` | TIMESTAMP | ✓ |
| 8 | `row_id` | STRING |  |
| 9 | `valid_from` | TIMESTAMP |  |
| 10 | `valid_to` | TIMESTAMP | ✓ |
| 11 | `is_current` | BOOL |  |
| 12 | `is_deleted` | BOOL |  |
| 13 | `record_hash` | STRING |  |
| 14 | `created_at` | TIMESTAMP |  |
| 15 | `updated_at` | TIMESTAMP |  |

### `dim_adset_meta` 🆕
**Clustered:** `is_current, adset_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `adset_id` | STRING |  |
| 2 | `campaign_id` | STRING | ✓ |
| 3 | `name` | STRING | ✓ |
| 4 | `status` | STRING | ✓ |
| 5 | `daily_budget` | FLOAT64 | ✓ |
| 6 | `lifetime_budget` | FLOAT64 | ✓ |
| 7 | `optimization_goal` | STRING | ✓ |
| 8 | `billing_event` | STRING | ✓ |
| 9 | `start_time` | TIMESTAMP | ✓ |
| 10 | `stop_time` | TIMESTAMP | ✓ |
| 11 | `meta_created_at` | TIMESTAMP | ✓ |
| 12 | `meta_updated_at` | TIMESTAMP | ✓ |
| 13 | `row_id` | STRING |  |
| 14 | `valid_from` | TIMESTAMP |  |
| 15 | `valid_to` | TIMESTAMP | ✓ |
| 16 | `is_current` | BOOL |  |
| 17 | `is_deleted` | BOOL |  |
| 18 | `record_hash` | STRING |  |
| 19 | `created_at` | TIMESTAMP |  |
| 20 | `updated_at` | TIMESTAMP |  |

### `dim_campaign` 🆕
**Clustered:** `is_current, campaign_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `campaign_id` | INT64 |  |
| 2 | `customer_id` | INT64 | ✓ |
| 3 | `campaign_name` | STRING | ✓ |
| 4 | `campaign_status` | STRING | ✓ |
| 5 | `campaign_serving_status` | STRING | ✓ |
| 6 | `campaign_advertising_channel_type` | STRING | ✓ |
| 7 | `campaign_advertising_channel_sub_type` | STRING | ✓ |
| 8 | `campaign_bidding_strategy_type` | STRING | ✓ |
| 9 | `bidding_strategy_name` | STRING | ✓ |
| 10 | `campaign_budget_amount_micros` | INT64 | ✓ |
| 11 | `campaign_budget_explicitly_shared` | BOOL | ✓ |
| 12 | `campaign_budget_period` | STRING | ✓ |
| 13 | `campaign_start_date` | DATE | ✓ |
| 14 | `campaign_end_date` | DATE | ✓ |
| 15 | `campaign_experiment_type` | STRING | ✓ |
| 16 | `campaign_maximize_conversion_value_target_roas` | FLOAT64 | ✓ |
| 17 | `row_id` | STRING |  |
| 18 | `valid_from` | TIMESTAMP |  |
| 19 | `valid_to` | TIMESTAMP | ✓ |
| 20 | `is_current` | BOOL |  |
| 21 | `is_deleted` | BOOL |  |
| 22 | `record_hash` | STRING |  |
| 23 | `created_at` | TIMESTAMP |  |
| 24 | `updated_at` | TIMESTAMP |  |

### `dim_campaign_meta` 🆕
**Clustered:** `is_current, campaign_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `campaign_id` | STRING |  |
| 2 | `name` | STRING | ✓ |
| 3 | `status` | STRING | ✓ |
| 4 | `objective` | STRING | ✓ |
| 5 | `buying_type` | STRING | ✓ |
| 6 | `daily_budget` | FLOAT64 | ✓ |
| 7 | `lifetime_budget` | FLOAT64 | ✓ |
| 8 | `start_time` | TIMESTAMP | ✓ |
| 9 | `stop_time` | TIMESTAMP | ✓ |
| 10 | `meta_created_at` | TIMESTAMP | ✓ |
| 11 | `meta_updated_at` | TIMESTAMP | ✓ |
| 12 | `row_id` | STRING |  |
| 13 | `valid_from` | TIMESTAMP |  |
| 14 | `valid_to` | TIMESTAMP | ✓ |
| 15 | `is_current` | BOOL |  |
| 16 | `is_deleted` | BOOL |  |
| 17 | `record_hash` | STRING |  |
| 18 | `created_at` | TIMESTAMP |  |
| 19 | `updated_at` | TIMESTAMP |  |

### `dim_channel`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `channel_key` | STRING | ✓ |
| 2 | `channel_name` | STRING | ✓ |
| 3 | `channel_type` | STRING | ✓ |
| 4 | `channel_category` | STRING | ✓ |
| 5 | `is_revenue_channel` | BOOL | ✓ |
| 6 | `is_ad_channel` | BOOL | ✓ |
| 7 | `is_active` | BOOL | ✓ |

### `dim_customer`
**Clustered:** `is_current, customer_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `customer_id` | INT64 |  |
| 2 | `email` | STRING | ✓ |
| 3 | `first_name` | STRING | ✓ |
| 4 | `last_name` | STRING | ✓ |
| 5 | `phone` | STRING | ✓ |
| 6 | `state` | STRING | ✓ |
| 7 | `tags` | STRING | ✓ |
| 8 | `verified_email` | BOOL | ✓ |
| 9 | `currency` | STRING | ✓ |
| 10 | `identity_key` | STRING | ✓ |
| 11 | `has_loop` | BOOL | ✓ |
| 12 | `shopify_created_at` | TIMESTAMP | ✓ |
| 13 | `shopify_updated_at` | TIMESTAMP | ✓ |
| 14 | `row_id` | STRING |  |
| 15 | `valid_from` | TIMESTAMP |  |
| 16 | `valid_to` | TIMESTAMP | ✓ |
| 17 | `is_current` | BOOL |  |
| 18 | `is_deleted` | BOOL |  |
| 19 | `record_hash` | STRING |  |
| 20 | `created_at` | TIMESTAMP |  |
| 21 | `updated_at` | TIMESTAMP |  |
| 22 | `ship_city` | STRING | ✓ |
| 23 | `ship_province` | STRING | ✓ |
| 24 | `ship_zip` | STRING | ✓ |
| 25 | `ship_country_code` | STRING | ✓ |

### `dim_date`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `date_key` | DATE | ✓ |
| 2 | `full_date` | DATE | ✓ |
| 3 | `year` | INT64 | ✓ |
| 4 | `month` | INT64 | ✓ |
| 5 | `day` | INT64 | ✓ |
| 6 | `day_of_week` | INT64 | ✓ |
| 7 | `day_of_year` | INT64 | ✓ |
| 8 | `week_of_year` | INT64 | ✓ |
| 9 | `quarter` | INT64 | ✓ |
| 10 | `day_name` | STRING | ✓ |
| 11 | `month_name` | STRING | ✓ |
| 12 | `year_month` | STRING | ✓ |
| 13 | `year_quarter` | STRING | ✓ |
| 14 | `is_weekend` | BOOL | ✓ |
| 15 | `is_today` | BOOL | ✓ |
| 16 | `is_past` | BOOL | ✓ |
| 17 | `is_future` | BOOL | ✓ |
| 18 | `week_start_date` | DATE | ✓ |
| 19 | `month_start_date` | DATE | ✓ |
| 20 | `quarter_start_date` | DATE | ✓ |
| 21 | `year_start_date` | DATE | ✓ |
| 22 | `month_end_date` | DATE | ✓ |
| 23 | `quarter_end_date` | DATE | ✓ |
| 24 | `year_end_date` | DATE | ✓ |
| 25 | `event_name` | STRING | ✓ |
| 26 | `event_type` | STRING | ✓ |

### `dim_location`
**Clustered:** `is_current, location_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `location_id` | INT64 |  |
| 2 | `location_type` | STRING | ✓ |
| 3 | `location_subtype` | STRING | ✓ |
| 4 | `location_name` | STRING | ✓ |
| 5 | `address_1` | STRING | ✓ |
| 6 | `address_2` | STRING | ✓ |
| 7 | `city` | STRING | ✓ |
| 8 | `state` | STRING | ✓ |
| 9 | `zip_code` | INT64 | ✓ |
| 10 | `servicing_rdc` | INT64 | ✓ |
| 11 | `servicing_fdc` | INT64 | ✓ |
| 12 | `region` | INT64 | ✓ |
| 13 | `district` | INT64 | ✓ |
| 14 | `group_id` | INT64 | ✓ |
| 15 | `store_format` | STRING | ✓ |
| 16 | `store_size` | INT64 | ✓ |
| 17 | `latitude` | FLOAT64 | ✓ |
| 18 | `longitude` | FLOAT64 | ✓ |
| 19 | `store_open_date` | DATE | ✓ |
| 20 | `store_close_date` | DATE | ✓ |
| 21 | `store_status` | STRING | ✓ |
| 22 | `optical_in_store` | BOOL | ✓ |
| 23 | `pharmacy_in_store` | BOOL | ✓ |
| 24 | `row_id` | STRING |  |
| 25 | `valid_from` | TIMESTAMP |  |
| 26 | `valid_to` | TIMESTAMP | ✓ |
| 27 | `is_current` | BOOL |  |
| 28 | `is_deleted` | BOOL |  |
| 29 | `record_hash` | STRING |  |
| 30 | `created_at` | TIMESTAMP |  |
| 31 | `updated_at` | TIMESTAMP |  |

### `dim_product`
**Clustered:** `is_current, product_key`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `product_key` | STRING |  |
| 2 | `sku` | STRING | ✓ |
| 3 | `product_title` | STRING | ✓ |
| 4 | `variant_title` | STRING | ✓ |
| 5 | `product_type` | STRING | ✓ |
| 6 | `vendor` | STRING | ✓ |
| 7 | `product_status` | STRING | ✓ |
| 8 | `current_price` | NUMERIC | ✓ |
| 9 | `shopify_variant_id` | STRING | ✓ |
| 10 | `inventory_item_id` | STRING | ✓ |
| 11 | `tcin` | STRING | ✓ |
| 12 | `manufacturer_style` | STRING | ✓ |
| 13 | `target_item_description` | STRING | ✓ |
| 14 | `target_dept` | STRING | ✓ |
| 15 | `target_class` | STRING | ✓ |
| 16 | `is_in_shopify` | BOOL | ✓ |
| 17 | `is_in_target` | BOOL | ✓ |
| 18 | `is_target_exclusive` | BOOL | ✓ |
| 19 | `is_discontinued` | BOOL | ✓ |
| 20 | `is_bundle` | BOOL | ✓ |
| 21 | `row_id` | STRING |  |
| 22 | `valid_from` | TIMESTAMP |  |
| 23 | `valid_to` | TIMESTAMP | ✓ |
| 24 | `is_current` | BOOL |  |
| 25 | `is_deleted` | BOOL |  |
| 26 | `record_hash` | STRING |  |
| 27 | `created_at` | TIMESTAMP |  |
| 28 | `updated_at` | TIMESTAMP |  |
| 29 | `product_category` | STRING | ✓ |
| 30 | `product_sub_category` | STRING | ✓ |

### `dim_product_discontinued` 🆕
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `sku` | STRING | ✓ |
| 2 | `product_title` | STRING | ✓ |
| 3 | `product_category` | STRING | ✓ |
| 4 | `sku_prefix_class` | STRING | ✓ |
| 5 | `is_confirmed_deleted` | BOOL | ✓ |
| 6 | `is_active_gap` | BOOL | ✓ |
| 7 | `first_order_date` | DATE | ✓ |
| 8 | `last_order_date` | DATE | ✓ |
| 9 | `lifetime_order_count` | INT64 | ✓ |
| 10 | `lifetime_customer_count` | INT64 | ✓ |
| 11 | `lifetime_gross_revenue` | NUMERIC | ✓ |
| 12 | `warehouse_loaded_at` | TIMESTAMP | ✓ |

### `dim_product_variant`
**Clustered:** `is_current, variant_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `variant_id` | INT64 |  |
| 2 | `product_id` | INT64 | ✓ |
| 3 | `variant_title` | STRING | ✓ |
| 4 | `product_title` | STRING | ✓ |
| 5 | `sku` | STRING | ✓ |
| 6 | `product_status` | STRING | ✓ |
| 7 | `is_order_active` | BOOL | ✓ |
| 8 | `price` | NUMERIC | ✓ |
| 9 | `compare_at_price` | NUMERIC | ✓ |
| 10 | `inventory_item_id` | INT64 | ✓ |
| 11 | `shopify_created_at` | TIMESTAMP | ✓ |
| 12 | `shopify_updated_at` | TIMESTAMP | ✓ |
| 13 | `row_id` | STRING |  |
| 14 | `valid_from` | TIMESTAMP |  |
| 15 | `valid_to` | TIMESTAMP | ✓ |
| 16 | `is_current` | BOOL |  |
| 17 | `is_deleted` | BOOL |  |
| 18 | `record_hash` | STRING |  |
| 19 | `created_at` | TIMESTAMP |  |
| 20 | `updated_at` | TIMESTAMP |  |

### `dim_subscription_plan`
**Clustered:** `is_current, selling_plan_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `selling_plan_id` | INT64 |  |
| 2 | `selling_plan_name` | STRING | ✓ |
| 3 | `selling_plan_group_name` | STRING | ✓ |
| 4 | `selling_plan_group_merchant_code` | STRING | ✓ |
| 5 | `row_id` | STRING |  |
| 6 | `valid_from` | TIMESTAMP |  |
| 7 | `valid_to` | TIMESTAMP | ✓ |
| 8 | `is_current` | BOOL |  |
| 9 | `is_deleted` | BOOL |  |
| 10 | `record_hash` | STRING |  |
| 11 | `created_at` | TIMESTAMP |  |
| 12 | `updated_at` | TIMESTAMP |  |

### `fct_ad_performance` 🆕
**Partitioned:** `date` · **Clustered:** `campaign_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `campaign_id` | INT64 |  |
| 2 | `date` | DATE |  |
| 3 | `device` | STRING |  |
| 4 | `network_type` | STRING |  |
| 5 | `customer_id` | INT64 | ✓ |
| 6 | `channel_key` | STRING |  |
| 7 | `spend_usd` | FLOAT64 | ✓ |
| 8 | `impressions` | INT64 | ✓ |
| 9 | `clicks` | INT64 | ✓ |
| 10 | `conversions` | FLOAT64 | ✓ |
| 11 | `conversions_value` | FLOAT64 | ✓ |
| 12 | `ctr` | FLOAT64 | ✓ |
| 13 | `average_cpc` | FLOAT64 | ✓ |
| 14 | `average_cpm` | FLOAT64 | ✓ |
| 15 | `row_id` | STRING |  |
| 16 | `loaded_at` | TIMESTAMP |  |

### `fct_delivery`
**Partitioned:** `DATE(shipment_created_at_utc)` · **Clustered:** `is_current, shipment_status`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `shipment_id` | STRING |  |
| 2 | `shopify_order_id` | STRING | ✓ |
| 3 | `malomo_order_id` | STRING | ✓ |
| 4 | `order_name` | STRING | ✓ |
| 5 | `tracking_number` | STRING | ✓ |
| 6 | `carrier` | STRING | ✓ |
| 7 | `shipment_status` | STRING | ✓ |
| 8 | `shipment_created_at_utc` | TIMESTAMP | ✓ |
| 9 | `delivered_at_utc` | TIMESTAMP | ✓ |
| 10 | `estimated_delivery_at_utc` | TIMESTAMP | ✓ |
| 11 | `days_to_delivery` | INT64 | ✓ |
| 12 | `order_created_at_utc` | TIMESTAMP | ✓ |
| 13 | `placed_at` | TIMESTAMP | ✓ |
| 14 | `fulfilled_at` | TIMESTAMP | ✓ |
| 15 | `delivery_lifecycle_status` | STRING | ✓ |
| 16 | `shipment_count` | INT64 | ✓ |
| 17 | `has_delivered_shipment` | INT64 | ✓ |
| 18 | `has_active_shipment` | INT64 | ✓ |
| 19 | `days_to_first_shipment` | INT64 | ✓ |
| 20 | `first_shipment_created_at_utc` | TIMESTAMP | ✓ |
| 21 | `latest_delivered_at_utc` | TIMESTAMP | ✓ |
| 22 | `shipment_updated_at_utc` | TIMESTAMP | ✓ |
| 23 | `row_id` | STRING |  |
| 24 | `valid_from` | TIMESTAMP |  |
| 25 | `valid_to` | TIMESTAMP | ✓ |
| 26 | `is_current` | BOOL |  |
| 27 | `is_deleted` | BOOL |  |
| 28 | `record_hash` | STRING |  |
| 29 | `created_at` | TIMESTAMP |  |
| 30 | `updated_at` | TIMESTAMP |  |

### `fct_inventory`
**Clustered:** `is_current, inventory_item_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `inventory_item_id` | INT64 |  |
| 2 | `location_id` | INT64 | ✓ |
| 3 | `location_name` | STRING | ✓ |
| 4 | `available_quantity` | INT64 | ✓ |
| 5 | `on_hand_quantity` | INT64 | ✓ |
| 6 | `sku` | STRING | ✓ |
| 7 | `cost` | NUMERIC | ✓ |
| 8 | `tracked` | BOOL | ✓ |
| 9 | `requires_shipping` | BOOL | ✓ |
| 10 | `country_code_of_origin` | STRING | ✓ |
| 11 | `province_code_of_origin` | STRING | ✓ |
| 12 | `harmonized_system_code` | STRING | ✓ |
| 13 | `item_created_at` | TIMESTAMP | ✓ |
| 14 | `item_updated_at` | TIMESTAMP | ✓ |
| 15 | `level_updated_at` | TIMESTAMP | ✓ |
| 16 | `row_id` | STRING |  |
| 17 | `valid_from` | TIMESTAMP |  |
| 18 | `valid_to` | TIMESTAMP | ✓ |
| 19 | `is_current` | BOOL |  |
| 20 | `is_deleted` | BOOL |  |
| 21 | `record_hash` | STRING |  |
| 22 | `created_at` | TIMESTAMP |  |
| 23 | `updated_at` | TIMESTAMP |  |

### `fct_keyword_performance` 🆕
**Partitioned:** `date` · **Clustered:** `campaign_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `campaign_id` | INT64 |  |
| 2 | `ad_group_id` | INT64 |  |
| 3 | `criterion_id` | INT64 |  |
| 4 | `date` | DATE |  |
| 5 | `device` | STRING |  |
| 6 | `customer_id` | INT64 | ✓ |
| 7 | `channel_key` | STRING |  |
| 8 | `spend_usd` | FLOAT64 | ✓ |
| 9 | `impressions` | INT64 | ✓ |
| 10 | `clicks` | INT64 | ✓ |
| 11 | `conversions` | FLOAT64 | ✓ |
| 12 | `conversions_value` | FLOAT64 | ✓ |
| 13 | `ctr` | FLOAT64 | ✓ |
| 14 | `average_cpc` | FLOAT64 | ✓ |
| 15 | `row_id` | STRING |  |
| 16 | `loaded_at` | TIMESTAMP |  |

### `fct_meta_performance` 🆕
**Partitioned:** `date_start` · **Clustered:** `ad_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `ad_id` | STRING |  |
| 2 | `date_start` | DATE |  |
| 3 | `device_platform` | STRING |  |
| 4 | `publisher_platform` | STRING |  |
| 5 | `adset_id` | STRING | ✓ |
| 6 | `campaign_id` | STRING | ✓ |
| 7 | `impressions` | INT64 | ✓ |
| 8 | `clicks` | INT64 | ✓ |
| 9 | `reach` | INT64 | ✓ |
| 10 | `spend` | FLOAT64 | ✓ |
| 11 | `purchases` | INT64 | ✓ |
| 12 | `purchase_value` | FLOAT64 | ✓ |
| 13 | `add_to_cart` | INT64 | ✓ |
| 14 | `view_content` | INT64 | ✓ |
| 15 | `cpc` | FLOAT64 | ✓ |
| 16 | `cpm` | FLOAT64 | ✓ |
| 17 | `ctr` | FLOAT64 | ✓ |
| 18 | `cpp` | FLOAT64 | ✓ |
| 19 | `frequency` | FLOAT64 | ✓ |
| 20 | `row_id` | STRING |  |
| 21 | `loaded_at` | TIMESTAMP |  |

### `fct_orders`
**Partitioned:** `DATE(order_created_at_utc)` · **Clustered:** `is_current, order_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `order_id` | STRING |  |
| 2 | `line_item_id` | STRING |  |
| 3 | `order_name` | STRING | ✓ |
| 4 | `customer_id` | INT64 | ✓ |
| 5 | `order_created_at_utc` | TIMESTAMP | ✓ |
| 6 | `order_created_datetime_ct` | DATETIME | ✓ |
| 7 | `order_updated_at_utc` | TIMESTAMP | ✓ |
| 8 | `financial_status` | STRING | ✓ |
| 9 | `fulfillment_status` | STRING | ✓ |
| 10 | `currency_code` | STRING | ✓ |
| 11 | `order_source` | STRING | ✓ |
| 12 | `order_tags_raw` | STRING | ✓ |
| 13 | `purchase_type` | STRING | ✓ |
| 14 | `current_total_price` | NUMERIC | ✓ |
| 15 | `current_subtotal_price` | NUMERIC | ✓ |
| 16 | `current_total_discounts` | NUMERIC | ✓ |
| 17 | `total_shipping` | NUMERIC | ✓ |
| 18 | `total_tax` | NUMERIC | ✓ |
| 19 | `product_id` | INT64 | ✓ |
| 20 | `variant_id` | INT64 | ✓ |
| 21 | `sku` | STRING | ✓ |
| 22 | `current_sku` | STRING | ✓ |
| 23 | `product_title` | STRING | ✓ |
| 24 | `variant_title` | STRING | ✓ |
| 25 | `quantity` | INT64 | ✓ |
| 26 | `unit_price` | NUMERIC | ✓ |
| 27 | `total_discount` | NUMERIC | ✓ |
| 28 | `gross_using_line_price` | NUMERIC | ✓ |
| 29 | `net_line_sales` | NUMERIC | ✓ |
| 30 | `row_id` | STRING |  |
| 31 | `valid_from` | TIMESTAMP |  |
| 32 | `valid_to` | TIMESTAMP | ✓ |
| 33 | `is_current` | BOOL |  |
| 34 | `is_deleted` | BOOL |  |
| 35 | `record_hash` | STRING |  |
| 36 | `created_at` | TIMESTAMP |  |
| 37 | `updated_at` | TIMESTAMP |  |
| 38 | `landing_site` | STRING | ✓ |
| 39 | `referring_site` | STRING | ✓ |

### `fct_refunds`
**Partitioned:** `DATE(refund_created_at_utc)` · **Clustered:** `is_current, order_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `refund_id` | INT64 |  |
| 2 | `order_id` | INT64 | ✓ |
| 3 | `refund_created_at_utc` | TIMESTAMP | ✓ |
| 4 | `note` | STRING | ✓ |
| 5 | `total_refunded` | NUMERIC | ✓ |
| 6 | `row_id` | STRING |  |
| 7 | `valid_from` | TIMESTAMP |  |
| 8 | `valid_to` | TIMESTAMP | ✓ |
| 9 | `is_current` | BOOL |  |
| 10 | `is_deleted` | BOOL |  |
| 11 | `record_hash` | STRING |  |
| 12 | `created_at` | TIMESTAMP |  |
| 13 | `updated_at` | TIMESTAMP |  |

### `fct_revenue`
**Partitioned:** `revenue_date` · **Clustered:** `is_current, channel_key`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `revenue_key` | STRING |  |
| 2 | `channel_key` | STRING |  |
| 3 | `revenue_date` | DATE |  |
| 4 | `revenue_amount` | NUMERIC | ✓ |
| 5 | `net_revenue_amount` | NUMERIC | ✓ |
| 6 | `units_sold` | INT64 | ✓ |
| 7 | `discount_amount` | NUMERIC | ✓ |
| 8 | `order_id` | STRING | ✓ |
| 9 | `line_item_id` | STRING | ✓ |
| 10 | `purchase_type` | STRING | ✓ |
| 11 | `financial_status` | STRING | ✓ |
| 12 | `order_source` | STRING | ✓ |
| 13 | `variant_id` | INT64 | ✓ |
| 14 | `product_id` | INT64 | ✓ |
| 15 | `tcin` | INT64 | ✓ |
| 16 | `location_id` | INT64 | ✓ |
| 17 | `origination_channel` | STRING | ✓ |
| 18 | `reporting_channel` | STRING | ✓ |
| 19 | `fulfillment_type` | STRING | ✓ |
| 20 | `manufacturer_style` | STRING | ✓ |
| 21 | `data_grain` | STRING | ✓ |
| 22 | `row_id` | STRING |  |
| 23 | `valid_from` | TIMESTAMP |  |
| 24 | `valid_to` | TIMESTAMP | ✓ |
| 25 | `is_current` | BOOL |  |
| 26 | `is_deleted` | BOOL |  |
| 27 | `record_hash` | STRING |  |
| 28 | `created_at` | TIMESTAMP |  |
| 29 | `updated_at` | TIMESTAMP |  |

### `fct_shopping_performance` 🆕
**Partitioned:** `date` · **Clustered:** `campaign_id`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `campaign_id` | INT64 |  |
| 2 | `item_id` | STRING |  |
| 3 | `date` | DATE |  |
| 4 | `device` | STRING |  |
| 5 | `product_channel` | STRING |  |
| 6 | `customer_id` | INT64 | ✓ |
| 7 | `ad_group_id` | INT64 | ✓ |
| 8 | `variant_id` | INT64 | ✓ |
| 9 | `channel_key` | STRING |  |
| 10 | `spend_usd` | FLOAT64 | ✓ |
| 11 | `impressions` | INT64 | ✓ |
| 12 | `clicks` | INT64 | ✓ |
| 13 | `conversions` | FLOAT64 | ✓ |
| 14 | `conversions_value` | FLOAT64 | ✓ |
| 15 | `all_conversions` | FLOAT64 | ✓ |
| 16 | `all_conversions_value` | FLOAT64 | ✓ |
| 17 | `cross_device_conversions` | FLOAT64 | ✓ |
| 18 | `ctr` | FLOAT64 | ✓ |
| 19 | `average_cpc` | FLOAT64 | ✓ |
| 20 | `search_impression_share` | FLOAT64 | ✓ |
| 21 | `search_click_share` | FLOAT64 | ✓ |
| 22 | `search_absolute_top_impression_share` | FLOAT64 | ✓ |
| 23 | `row_id` | STRING |  |
| 24 | `loaded_at` | TIMESTAMP |  |

### `fct_subscription_events`
**Partitioned:** `DATE(event_at)` · **Clustered:** `event_type, channel_key`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `subscription_id` | STRING |  |
| 2 | `event_type` | STRING |  |
| 3 | `event_at` | TIMESTAMP |  |
| 4 | `customer_id` | INT64 | ✓ |
| 5 | `variant_id` | INT64 | ✓ |
| 6 | `selling_plan_id` | INT64 | ✓ |
| 7 | `cancellation_reason` | STRING | ✓ |
| 8 | `cancellation_comment` | STRING | ✓ |
| 9 | `billing_interval` | STRING | ✓ |
| 10 | `billing_interval_count` | INT64 | ✓ |
| 11 | `origin_order_shopify_id` | STRING | ✓ |
| 12 | `is_loop_subscription_certified` | BOOL | ✓ |
| 13 | `channel_key` | STRING |  |
| 14 | `row_id` | STRING |  |
| 15 | `loaded_at` | TIMESTAMP |  |

### `fct_subscriptions`
**Clustered:** `is_current, status`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `subscription_id` | STRING |  |
| 2 | `shopify_subscription_id` | STRING | ✓ |
| 3 | `origin_order_shopify_id` | STRING | ✓ |
| 4 | `customer_id` | INT64 | ✓ |
| 5 | `identity_key` | STRING | ✓ |
| 6 | `status` | STRING | ✓ |
| 7 | `currency` | STRING | ✓ |
| 8 | `subscription_created_at` | TIMESTAMP | ✓ |
| 9 | `total_line_item_price` | NUMERIC | ✓ |
| 10 | `total_line_item_discounted_price` | NUMERIC | ✓ |
| 11 | `delivery_price` | NUMERIC | ✓ |
| 12 | `cancellation_reason` | STRING | ✓ |
| 13 | `cancellation_comment` | STRING | ✓ |
| 14 | `paused_at` | TIMESTAMP | ✓ |
| 15 | `cancelled_at` | TIMESTAMP | ✓ |
| 16 | `is_prepaid` | BOOL | ✓ |
| 17 | `is_marked_for_cancellation` | BOOL | ✓ |
| 18 | `last_payment_status` | STRING | ✓ |
| 19 | `last_inventory_action` | STRING | ✓ |
| 20 | `delivery_method_code` | STRING | ✓ |
| 21 | `delivery_method_title` | STRING | ✓ |
| 22 | `billing_interval` | STRING | ✓ |
| 23 | `billing_interval_count` | INT64 | ✓ |
| 24 | `delivery_interval` | STRING | ✓ |
| 25 | `delivery_interval_count` | INT64 | ✓ |
| 26 | `shipping_city` | STRING | ✓ |
| 27 | `shipping_zip` | STRING | ✓ |
| 28 | `shipping_country_code` | STRING | ✓ |
| 29 | `shipping_province_code` | STRING | ✓ |
| 30 | `payment_method_type` | STRING | ✓ |
| 31 | `payment_method_status` | STRING | ✓ |
| 32 | `payment_method_source` | STRING | ✓ |
| 33 | `is_migrated` | BOOL | ✓ |
| 34 | `is_loop_revenue_certified` | BOOL | ✓ |
| 35 | `selling_plan_id` | INT64 | ✓ |
| 36 | `variant_id` | INT64 | ✓ |
| 37 | `is_loop_subscription_certified` | BOOL | ✓ |
| 38 | `next_order_date` | TIMESTAMP | ✓ |
| 39 | `completed_orders_count` | INT64 | ✓ |
| 40 | `subscription_updated_at` | TIMESTAMP | ✓ |
| 41 | `is_current_partial_month` | BOOL | ✓ |
| 42 | `is_mass_update_month` | BOOL | ✓ |
| 43 | `row_id` | STRING |  |
| 44 | `valid_from` | TIMESTAMP |  |
| 45 | `valid_to` | TIMESTAMP | ✓ |
| 46 | `is_current` | BOOL |  |
| 47 | `is_deleted` | BOOL |  |
| 48 | `record_hash` | STRING |  |
| 49 | `created_at` | TIMESTAMP |  |
| 50 | `updated_at` | TIMESTAMP |  |

### `fct_target_gross_margin`
**Partitioned:** `fiscal_week_end_date`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `fiscal_week_end_date` | DATE | ✓ |
| 2 | `tcin` | INT64 | ✓ |
| 3 | `dpci` | STRING | ✓ |
| 4 | `channel_originated` | STRING | ✓ |
| 5 | `location_id_originated` | INT64 | ✓ |
| 6 | `location_id` | INT64 | ✓ |
| 7 | `channel_fulfilled` | STRING | ✓ |
| 8 | `fulfillment_type` | STRING | ✓ |
| 9 | `fulfillment_subtype` | STRING | ✓ |
| 10 | `department_id` | INT64 | ✓ |
| 11 | `class_id` | INT64 | ✓ |
| 12 | `item_id` | INT64 | ✓ |
| 13 | `net_sales_a` | FLOAT64 | ✓ |
| 14 | `adjusted_gross_margin_a` | FLOAT64 | ✓ |
| 15 | `ytd_adjusted_gross_margin_a` | FLOAT64 | ✓ |
| 16 | `adjusted_gross_margin_with_net_ship_margin_a` | FLOAT64 | ✓ |
| 17 | `row_id` | STRING | ✓ |
| 18 | `valid_from` | TIMESTAMP | ✓ |
| 19 | `valid_to` | TIMESTAMP | ✓ |
| 20 | `is_current` | BOOL | ✓ |
| 21 | `is_deleted` | BOOL | ✓ |
| 22 | `record_hash` | STRING | ✓ |
| 23 | `created_at` | TIMESTAMP | ✓ |
| 24 | `updated_at` | TIMESTAMP | ✓ |

### `fct_target_inventory`
**Partitioned:** `inventory_date` · **Clustered:** `is_current, tcin`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `inventory_date` | DATE |  |
| 2 | `tcin` | INT64 |  |
| 3 | `location_id` | INT64 |  |
| 4 | `primary_vendor_id` | INT64 | ✓ |
| 5 | `department_id` | INT64 | ✓ |
| 6 | `class_id` | INT64 | ✓ |
| 7 | `dpci` | STRING | ✓ |
| 8 | `manufacturer_style` | STRING | ✓ |
| 9 | `item_description` | STRING | ✓ |
| 10 | `ending_on_hand_a` | FLOAT64 | ✓ |
| 11 | `ending_on_hand_q` | INT64 | ✓ |
| 12 | `ending_on_transfer_a` | FLOAT64 | ✓ |
| 13 | `ending_on_transfer_q` | INT64 | ✓ |
| 14 | `ending_on_purchase_a` | FLOAT64 | ✓ |
| 15 | `ending_on_purchase_q` | INT64 | ✓ |
| 16 | `instock_q` | INT64 | ✓ |
| 17 | `instock_percentage` | FLOAT64 | ✓ |
| 18 | `out_of_stock_q` | INT64 | ✓ |
| 19 | `out_of_stock_percentage` | FLOAT64 | ✓ |
| 20 | `tracked_item_out_of_stock_q` | FLOAT64 | ✓ |
| 21 | `data_grain` | STRING |  |
| 22 | `row_id` | STRING |  |
| 23 | `valid_from` | TIMESTAMP |  |
| 24 | `valid_to` | TIMESTAMP | ✓ |
| 25 | `is_current` | BOOL |  |
| 26 | `is_deleted` | BOOL |  |
| 27 | `record_hash` | STRING |  |
| 28 | `created_at` | TIMESTAMP |  |
| 29 | `updated_at` | TIMESTAMP |  |

### `fct_target_sales`
**Partitioned:** `sales_date` · **Clustered:** `is_current, tcin`
*Judgment/quirks: see `database_reference.md`.*

| # | column | type | null |
|---|---|---|---|
| 1 | `sales_date` | DATE |  |
| 2 | `tcin` | INT64 |  |
| 3 | `location_id` | INT64 |  |
| 4 | `origination_channel` | STRING |  |
| 5 | `reporting_channel` | STRING |  |
| 6 | `fulfillment_type` | STRING |  |
| 7 | `vendor_id` | INT64 | ✓ |
| 8 | `barcode` | INT64 | ✓ |
| 9 | `dpci` | STRING | ✓ |
| 10 | `manufacturer_style` | STRING | ✓ |
| 11 | `dept` | INT64 | ✓ |
| 12 | `class` | INT64 | ✓ |
| 13 | `item_description` | STRING | ✓ |
| 14 | `original_location_id` | INT64 | ✓ |
| 15 | `original_reporting_channel` | STRING | ✓ |
| 16 | `original_origination_channel` | STRING | ✓ |
| 17 | `sale_amount` | FLOAT64 | ✓ |
| 18 | `sale_quantity` | FLOAT64 | ✓ |
| 19 | `circular_sale_amount` | FLOAT64 | ✓ |
| 20 | `circular_sale_quantity` | FLOAT64 | ✓ |
| 21 | `clearance_sale_amount` | FLOAT64 | ✓ |
| 22 | `clearance_sale_quantity` | FLOAT64 | ✓ |
| 23 | `promo_sale_amount` | FLOAT64 | ✓ |
| 24 | `promo_sale_quantity` | FLOAT64 | ✓ |
| 25 | `regular_sale_amount` | FLOAT64 | ✓ |
| 26 | `regular_sale_quantity` | FLOAT64 | ✓ |
| 27 | `circle_sale_amount` | FLOAT64 | ✓ |
| 28 | `circle_sale_quantity` | FLOAT64 | ✓ |
| 29 | `mature_sale_amount` | FLOAT64 | ✓ |
| 30 | `mature_sale_quantity` | FLOAT64 | ✓ |
| 31 | `comparable_sale_amount` | FLOAT64 | ✓ |
| 32 | `comparable_sale_quantity` | FLOAT64 | ✓ |
| 33 | `ad_comparable_sale_amount` | FLOAT64 | ✓ |
| 34 | `ad_comparable_sale_quantity` | FLOAT64 | ✓ |
| 35 | `return_guest_amount` | FLOAT64 | ✓ |
| 36 | `return_guest_quantity` | FLOAT64 | ✓ |
| 37 | `drive_up_sale_a` | FLOAT64 | ✓ |
| 38 | `drive_up_sale_q` | FLOAT64 | ✓ |
| 39 | `shipt_app_sale_a` | FLOAT64 | ✓ |
| 40 | `shipt_app_sale_q` | FLOAT64 | ✓ |
| 41 | `shipt_target_sale_a` | FLOAT64 | ✓ |
| 42 | `shipt_target_sale_q` | FLOAT64 | ✓ |
| 43 | `data_grain` | STRING |  |
| 44 | `row_id` | STRING |  |
| 45 | `valid_from` | TIMESTAMP |  |
| 46 | `valid_to` | TIMESTAMP | ✓ |
| 47 | `is_current` | BOOL |  |
| 48 | `is_deleted` | BOOL |  |
| 49 | `record_hash` | STRING |  |
| 50 | `created_at` | TIMESTAMP |  |
| 51 | `updated_at` | TIMESTAMP |  |

---

## Views (with logic)

View DDL is included because it encodes derivations and upstream lineage. Views already filter `is_current` internally — **do not re-add it** (Rule 1 exception in `database_reference.md`).

### `vw_inventory_oos` 🆕
Columns: `sku`, `oos_class`, `is_oos_all_listings`, `is_stockout`, `is_committed_only`, `units_sold_90d`, `is_selling_90d`, `listings_in_scope`, `listings_total`, `listings_out_of_scope`, `max_available`, `min_available`, `total_available`, `max_on_hand`, `total_on_hand`, `has_negative_available`

```sql
WITH

-- Catalogue status per inventory item.
-- QUALIFY is a guard, not a fix for an observed problem: inventory_item_id is
-- 1:1 with variant_id in Shopify by design and verified 1:1 live (2026-08-17).
-- A fan-out here would silently double-count listings.
variant_status AS (
  SELECT
    inventory_item_id,
    product_status,
    is_order_active
  FROM `biom-reporting-s26.biom_canvas.dim_product_variant`
  WHERE is_current = TRUE
    AND inventory_item_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY inventory_item_id ORDER BY variant_id
  ) = 1
),

-- Every current inventory row, with catalogue status attached.
-- Join is 0-miss on 725/725 current rows (verified 2026-08-17).
all_listings AS (
  SELECT
    f.inventory_item_id,
    f.sku,
    f.tracked,
    f.available_quantity,
    f.on_hand_quantity,
    v.product_status,
    v.is_order_active
  FROM `biom-reporting-s26.biom_canvas.fct_inventory` f
  LEFT JOIN variant_status v
    ON f.inventory_item_id = v.inventory_item_id
  WHERE f.is_current = TRUE
    AND f.is_deleted = FALSE
),

-- Total listing count per SKU BEFORE scoping, so a consumer can see how much
-- of a SKU's footprint the scope filter removed. This is the duplicate-listing
-- spread that produced artifact A.
listing_footprint AS (
  SELECT sku, COUNT(*) AS listings_total
  FROM all_listings
  WHERE sku IS NOT NULL
  GROUP BY sku
),

-- THE SCOPE. Correction 3 above.
scoped AS (
  SELECT *
  FROM all_listings
  WHERE product_status = 'active'
    AND is_order_active = TRUE
    AND sku IS NOT NULL
    -- F1: exclude tracked = FALSE. Shopify does not decrement untracked items,
    -- so their quantities are not meaningful in any aggregate (x-redo sits at
    -- a flat 1,000,000 forever, which is what made SUM(available_quantity)
    -- return 57,107,094). Preferred over a 9th copy of item 42's named
    -- non-product exclusion list because `tracked` is Shopify's own "this
    -- number means nothing" marker and needs no maintenance when the next
    -- checkout app is installed.
    -- ⚠️ Currently a NO-OP: 0 of the 239 sellable rows are untracked
    -- (verified 2026-08-17). Kept as a guard against a future active-and-
    -- untracked service SKU, and because F1 is correct on its own terms.
    -- It is NOT what removes the archived block -- the scope filter above is.
    AND tracked = TRUE
),

-- 90-day demand, for the L5/L6 re-basing. Uses resolved_sku, not the raw
-- order-time sku: vw_order_line_sku_resolved is the documented resolution path
-- and recovers lines whose variant_id was deleted from the catalogue.
sales_90d AS (
  SELECT
    r.resolved_sku AS sku,
    SUM(o.quantity) AS units_sold_90d
  FROM `biom-reporting-s26.biom_canvas.vw_order_line_sku_resolved` r
  JOIN `biom-reporting-s26.biom_canvas.fct_orders` o
    ON  o.order_id     = r.order_id
    AND o.line_item_id = r.line_item_id
    AND o.is_current   = TRUE
    AND o.is_deleted   = FALSE
  WHERE r.resolved_sku IS NOT NULL
    AND DATE(r.order_created_at_utc)
        BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) AND CURRENT_DATE()
  GROUP BY r.resolved_sku
),

sku_grain AS (
  SELECT
    s.sku,
    COUNT(*)                        AS listings_in_scope,
    -- Correction 1: MAX drives all-listings-OOS. A SKU is out of stock only
    -- when its BEST in-scope listing is <= 0.
    MAX(s.available_quantity)       AS max_available,
    MIN(s.available_quantity)       AS min_available,
    SUM(s.available_quantity)       AS total_available,
    MAX(s.on_hand_quantity)         AS max_on_hand,
    SUM(s.on_hand_quantity)         AS total_on_hand
  FROM scoped s
  GROUP BY s.sku
)

SELECT
  g.sku,

  -- ── The metric ──────────────────────────────────────────────────────────
  -- Correction 2: STOCKOUT vs COMMITTED_ONLY are different facts.
  CASE
    WHEN g.max_available <= 0 AND g.max_on_hand <= 0 THEN 'STOCKOUT'
    WHEN g.max_available <= 0 AND g.max_on_hand >  0 THEN 'COMMITTED_ONLY'
    ELSE 'IN_STOCK'
  END                                          AS oos_class,

  g.max_available <= 0                         AS is_oos_all_listings,
  (g.max_available <= 0 AND g.max_on_hand <= 0) AS is_stockout,
  (g.max_available <= 0 AND g.max_on_hand >  0) AS is_committed_only,

  -- ── Demand, for L5/L6 re-basing ─────────────────────────────────────────
  COALESCE(s.units_sold_90d, 0)                AS units_sold_90d,
  COALESCE(s.units_sold_90d, 0) > 0            AS is_selling_90d,

  -- ── Listing spread (artifact A visibility) ──────────────────────────────
  g.listings_in_scope,
  f.listings_total,
  f.listings_total - g.listings_in_scope       AS listings_out_of_scope,

  -- ── Quantities. Safe to aggregate: untracked rows are already excluded. ──
  g.max_available,
  g.min_available,
  g.total_available,
  g.max_on_hand,
  g.total_on_hand,

  -- ── Oversell flag (partial F4 — sellable subset only) ───────────────────
  g.min_available < 0                          AS has_negative_available

FROM sku_grain g
LEFT JOIN listing_footprint f ON f.sku = g.sku
LEFT JOIN sales_90d         s ON s.sku = g.sku
```

### `vw_media_delivery_status` 🆕
Columns: `channel`, `canvas_table`, `event_date`, `delivery_status`, `spend_modelled`, `impressions_modelled`, `evidence_method`, `evidence_ref`, `evidence_note`, `confidence`

```sql
WITH meta_span AS (
  SELECT MIN(date_start) AS d0, MAX(date_start) AS d1
  FROM `biom-reporting-s26.biom_canvas.fct_meta_performance`
),
meta_cal AS (
  SELECT d AS event_date
  FROM meta_span, UNNEST(GENERATE_DATE_ARRAY(d0, d1)) AS d
),
meta_observed AS (
  SELECT date_start AS event_date,
         COUNT(*)          AS n_rows,
         SUM(impressions)  AS impressions,
         SUM(spend)         AS spend
  FROM `biom-reporting-s26.biom_canvas.fct_meta_performance`
  GROUP BY event_date
),
meta_rows AS (
  SELECT
    'META'                  AS channel,
    'fct_meta_performance'  AS canvas_table,
    c.event_date,
    CASE
      WHEN o.n_rows > 0 AND o.impressions > 0 THEN 'DELIVERED'
      WHEN o.n_rows > 0                        THEN 'OBSERVED_ZERO'
      WHEN s.delivery_status IS NOT NULL       THEN s.delivery_status
      ELSE                                          'ABSENT_UNDIAGNOSED'
    END                      AS delivery_status,
    IFNULL(o.spend, 0)       AS spend_modelled,
    IFNULL(o.impressions, 0) AS impressions_modelled,
    s.evidence_method, s.evidence_ref, s.evidence_note, s.confidence
  FROM meta_cal c
  LEFT JOIN meta_observed o
    ON o.event_date = c.event_date
  LEFT JOIN `biom-reporting-s26.biom_admin.seed_media_delivery_status` s
    ON s.canvas_table = 'fct_meta_performance' AND s.event_date = c.event_date
),

google_span AS (
  SELECT MIN(date) AS d0, MAX(date) AS d1
  FROM `biom-reporting-s26.biom_canvas.fct_ad_performance`
),
google_cal AS (
  SELECT d AS event_date
  FROM google_span, UNNEST(GENERATE_DATE_ARRAY(d0, d1)) AS d
),
google_observed AS (
  SELECT date AS event_date,
         COUNT(*)          AS n_rows,
         SUM(impressions)  AS impressions,
         SUM(spend_usd)    AS spend
  FROM `biom-reporting-s26.biom_canvas.fct_ad_performance`
  GROUP BY event_date
),
google_rows AS (
  SELECT
    'GOOGLE'               AS channel,
    'fct_ad_performance'   AS canvas_table,
    c.event_date,
    CASE
      WHEN o.n_rows > 0 AND o.impressions > 0 THEN 'DELIVERED'
      WHEN o.n_rows > 0                        THEN 'OBSERVED_ZERO'
      WHEN s.delivery_status IS NOT NULL       THEN s.delivery_status
      ELSE                                          'ABSENT_UNDIAGNOSED'
    END                      AS delivery_status,
    IFNULL(o.spend, 0)       AS spend_modelled,
    IFNULL(o.impressions, 0) AS impressions_modelled,
    s.evidence_method, s.evidence_ref, s.evidence_note, s.confidence
  FROM google_cal c
  LEFT JOIN google_observed o
    ON o.event_date = c.event_date
  LEFT JOIN `biom-reporting-s26.biom_admin.seed_media_delivery_status` s
    ON s.canvas_table = 'fct_ad_performance' AND s.event_date = c.event_date
)

SELECT * FROM meta_rows
UNION ALL
SELECT * FROM google_rows
```

### `vw_mmm_phase3_weekly_dataset` 🆕
Columns: `week_start`, `gross_revenue`, `orders`, `new_customer_orders`, `meta_spend`, `meta_impressions`, `goog_spend`, `goog_impressions`, `disc_depth`, `sub_share`, `price`, `holiday_day_count`, `holiday_event_types`, `h10_era`

```sql
WITH week_spine AS (
  SELECT DATE_ADD('2025-07-07', INTERVAL 7*n DAY) AS week_start
  FROM UNNEST(GENERATE_ARRAY(0, 55)) AS n
),

media_pivot AS (
  SELECT
    event_date,
    MAX(IF(channel = 'META',   delivery_status,      NULL)) AS meta_status,
    MAX(IF(channel = 'META',   spend_modelled,        NULL)) AS meta_spend_raw,
    MAX(IF(channel = 'META',   impressions_modelled,  NULL)) AS meta_impr_raw,
    MAX(IF(channel = 'GOOGLE', delivery_status,      NULL)) AS goog_status,
    MAX(IF(channel = 'GOOGLE', spend_modelled,        NULL)) AS goog_spend_raw,
    MAX(IF(channel = 'GOOGLE', impressions_modelled,  NULL)) AS goog_impr_raw
  FROM `biom-reporting-s26.biom_canvas.vw_media_delivery_status`
  WHERE event_date BETWEEN '2025-07-07' AND '2026-08-02'
  GROUP BY event_date
),
media_safe AS (
  SELECT
    event_date,
    CASE WHEN meta_status IN ('DELIVERED','OBSERVED_ZERO','CONFIRMED_NO_DELIVERY') THEN meta_spend_raw END AS meta_spend_safe,
    CASE WHEN meta_status IN ('DELIVERED','OBSERVED_ZERO','CONFIRMED_NO_DELIVERY') THEN meta_impr_raw END AS meta_impr_safe,
    CASE WHEN goog_status IN ('DELIVERED','OBSERVED_ZERO','CONFIRMED_NO_DELIVERY') THEN goog_spend_raw END AS goog_spend_safe,
    CASE WHEN goog_status IN ('DELIVERED','OBSERVED_ZERO','CONFIRMED_NO_DELIVERY') THEN goog_impr_raw END AS goog_impr_safe
  FROM media_pivot
),
media_weekly AS (
  SELECT
    DATE_TRUNC(event_date, WEEK(MONDAY)) AS week_start,
    CASE WHEN COUNTIF(meta_spend_safe IS NULL) = 0 THEN SUM(meta_spend_safe) END AS meta_spend,
    CASE WHEN COUNTIF(meta_impr_safe  IS NULL) = 0 THEN SUM(meta_impr_safe)  END AS meta_impressions,
    CASE WHEN COUNTIF(goog_spend_safe IS NULL) = 0 THEN SUM(goog_spend_safe) END AS goog_spend,
    CASE WHEN COUNTIF(goog_impr_safe  IS NULL) = 0 THEN SUM(goog_impr_safe)  END AS goog_impressions
  FROM media_safe
  GROUP BY 1
),

rev AS (
  SELECT
    DATE_TRUNC(revenue_date, WEEK(MONDAY)) AS week_start,
    COUNT(DISTINCT order_id) AS orders,
    SUM(revenue_amount) AS gross_revenue,
    SAFE_DIVIDE(SUM(allocated_discount), SUM(revenue_amount)) AS disc_depth,
    SAFE_DIVIDE(SUM(IF(purchase_type = 'Subscription', admin_net_revenue, 0)), SUM(admin_net_revenue)) AS sub_share,
    SAFE_DIVIDE(SUM(unit_price * quantity), SUM(quantity)) AS price
  FROM `biom-reporting-s26.biom_canvas.vw_revenue_subscriptions`
  WHERE record_type = 'revenue' AND channel_key = 'shopify'
    AND revenue_date BETWEEN '2025-07-07' AND '2026-08-02'
  GROUP BY 1
),

first_orders AS (
  SELECT
    customer_id,
    MIN(DATE(order_created_at_utc, 'America/Chicago')) AS first_order_date
  FROM `biom-reporting-s26.biom_canvas.fct_orders`
  WHERE is_current = TRUE AND customer_id IS NOT NULL
  GROUP BY 1
),
new_customer_orders_weekly AS (
  SELECT
    DATE_TRUNC(DATE(o.order_created_at_utc, 'America/Chicago'), WEEK(MONDAY)) AS week_start,
    COUNT(DISTINCT o.order_id) AS new_customer_orders
  FROM `biom-reporting-s26.biom_canvas.fct_orders` o
  JOIN first_orders fo ON o.customer_id = fo.customer_id
  WHERE o.is_current = TRUE AND o.customer_id IS NOT NULL
    AND DATE(o.order_created_at_utc, 'America/Chicago') = fo.first_order_date
    AND DATE(o.order_created_at_utc, 'America/Chicago') BETWEEN '2025-07-07' AND '2026-08-02'
  GROUP BY 1
),

holiday_weekly AS (
  SELECT
    DATE_TRUNC(date_key, WEEK(MONDAY)) AS week_start,
    COUNTIF(event_type IS NOT NULL) AS holiday_day_count,
    STRING_AGG(DISTINCT event_type, '+' ORDER BY event_type) AS holiday_event_types
  FROM `biom-reporting-s26.biom_canvas.dim_date`
  WHERE date_key BETWEEN '2025-07-07' AND '2026-08-02'
  GROUP BY 1
)

SELECT
  ws.week_start,
  rev.gross_revenue,
  rev.orders,
  ncw.new_customer_orders,
  mw.meta_spend, mw.meta_impressions,
  mw.goog_spend, mw.goog_impressions,
  rev.disc_depth, rev.sub_share, rev.price,
  hw.holiday_day_count, hw.holiday_event_types,
  IF(ws.week_start BETWEEN '2025-07-07' AND '2025-09-08', 1, 0) AS h10_era
FROM week_spine ws
LEFT JOIN media_weekly mw ON mw.week_start = ws.week_start
LEFT JOIN rev ON rev.week_start = ws.week_start
LEFT JOIN new_customer_orders_weekly ncw ON ncw.week_start = ws.week_start
LEFT JOIN holiday_weekly hw ON hw.week_start = ws.week_start
ORDER BY ws.week_start
```

### `vw_order_line_sku_resolved` 🆕
Columns: `order_id`, `line_item_id`, `customer_id`, `order_created_at_utc`, `order_time_sku`, `line_variant_id`, `gross_using_line_price`, `resolution_method`, `resolved_variant_id`, `resolved_sku`, `candidate_variant_ids`

```sql
WITH
-- Authoritative non-product SKU set. Union of the three places this platform already
-- encodes it, so the definition cannot drift away from them:
--   canvas_delta.py get_source_dim_product()  -> '1111','2222','P-40WIP-LIT-FRA', vendor='re:do' (sku 'x-redo')
--   001_build_shopify_gross_sales_by_sku_certified.sql -> 'ONWARDINS01','1111','2222'
--   vw_repurchase_base 'Non-product line item' -> 'ONWARDINS01','fondue-cashback-sku-1665073709'
non_product_sku AS (
  SELECT sku FROM UNNEST([
    'ONWARDINS01',                     -- Onward shipping insurance ("Checkout+")
    'fondue-cashback-sku-1665073709',  -- Fondue cashback app
    'x-redo',                          -- Redo return-protection add-on (vendor='re:do')
    '1111', '2222',                    -- internal test SKUs
    'P-40WIP-LIT-FRA'                  -- "Wipes - Internal Use" internal artifact
  ]) AS sku
),

-- Every (historical sku -> LIVE variant) mapping, with the period that mapping was valid.
-- Joining to dim_product_variant on is_current here is what collapses most apparent
-- ambiguity: a sku reused across one live and several dead variants is not ambiguous.
hist_live AS (
  SELECT
    s.sku,
    SAFE_CAST(s.variant_id AS INT64)               AS variant_id,
    s.period_start,
    IFNULL(s.period_end, DATE '2099-12-31')        AS period_end,
    v.sku                                          AS target_sku
  FROM `biom-reporting-s26.biom_admin.sku_change_log` s
  JOIN `biom-reporting-s26.biom_canvas.dim_product_variant` v
    ON v.variant_id = SAFE_CAST(s.variant_id AS INT64)
   AND v.is_current
),

-- Per-sku rollup, date-agnostic. Used as the fallback tier when no period covers the
-- order date (sku_change_log periods are largely source='order_proxy' and do not
-- blanket every order date).
live_by_sku AS (
  SELECT
    sku,
    COUNT(DISTINCT variant_id) AS n_variants,
    COUNT(DISTINCT target_sku) AS n_targets,
    MIN(variant_id)            AS pick_variant_id,   -- deterministic; only used when n_targets = 1
    MIN(target_sku)            AS pick_target_sku,
    ARRAY_AGG(DISTINCT variant_id ORDER BY variant_id) AS candidate_variant_ids
  FROM hist_live
  GROUP BY sku
),

-- FIX 2 (2026-08-03): earliest recorded period per (sku, live variant). sku_change_log's
-- order_proxy periods are inferred from observed activity and do not reach back to a SKU's
-- first-ever order, so historical lines can fall before every recorded period. Rather than
-- touch sku_change_log (rebuilt daily 10:45 UTC, other consumers depend on it), the earliest
-- period is extended backwards here.
--
-- "Tied for first" uses a 7-day window rather than a strict MIN, because a strict MIN silently
-- picks the wrong product when two variants start within days of each other during a labelling
-- collision: K-WIP-AP-LAV-6PK's Fragrance-free variant starts 2024-05-30 and its Lavender
-- variant 2024-06-01, so strict-MIN would book Lavender revenue as Fragrance-free. Ties stay
-- AMBIGUOUS instead.
-- ponytail: 7-day tie window is a calibration constant, not a derived value. Widen it if a
-- genuine rename is ever observed taking longer than a week to propagate.
first_live AS (
  SELECT
    sku,
    variant_id,
    MIN(period_start) AS first_start,
    MIN(target_sku)   AS target_sku
  FROM hist_live
  GROUP BY sku, variant_id
),

backdate AS (
  SELECT
    sku,
    COUNT(DISTINCT variant_id) AS tied_variants,
    COUNT(DISTINCT target_sku) AS tied_targets,
    MIN(variant_id)            AS pick_variant_id,
    MIN(target_sku)            AS pick_target_sku
  FROM (
    SELECT fl.*, MIN(fl.first_start) OVER (PARTITION BY fl.sku) AS sku_min_start
    FROM first_live fl
  )
  WHERE DATE_DIFF(first_start, sku_min_start, DAY) <= 7
  GROUP BY sku
),

-- SKUs that map ONLY to dead variants — gives the DEAD_VARIANT tier a real id to emit
-- instead of a silent NULL.
dead_by_sku AS (
  SELECT
    s.sku,
    MIN(SAFE_CAST(s.variant_id AS INT64)) AS variant_id
  FROM `biom-reporting-s26.biom_admin.sku_change_log` s
  WHERE NOT EXISTS (
    SELECT 1 FROM `biom-reporting-s26.biom_canvas.dim_product_variant` v
    WHERE v.variant_id = SAFE_CAST(s.variant_id AS INT64) AND v.is_current
  )
  GROUP BY s.sku
),

-- Per-LINE rollup restricted to mappings whose period covers that line's order date.
-- GROUP BY the natural key is what guarantees 1:1 — no QUALIFY needed, and no join
-- downstream can multiply rows.
dated AS (
  SELECT
    o.order_id,
    o.line_item_id,
    COUNT(DISTINCT h.variant_id) AS n_variants,
    COUNT(DISTINCT h.target_sku) AS n_targets,
    MIN(h.variant_id)            AS pick_variant_id,
    MIN(h.target_sku)            AS pick_target_sku,
    ARRAY_AGG(DISTINCT h.variant_id IGNORE NULLS ORDER BY h.variant_id) AS candidate_variant_ids
  FROM `biom-reporting-s26.biom_canvas.fct_orders` o
  LEFT JOIN hist_live h
    ON h.sku = o.sku
   AND DATE(o.order_created_at_utc) BETWEEN h.period_start AND h.period_end
  WHERE o.is_current
  GROUP BY o.order_id, o.line_item_id
),

-- Does the line's own variant_id point at a live catalog row?
resolved AS (
  SELECT
    o.order_id,
    o.line_item_id,
    o.customer_id,
    o.order_created_at_utc,
    o.sku                                       AS order_time_sku,
    o.variant_id                                AS line_variant_id,
    o.gross_using_line_price,
    live.sku                                    AS live_sku_of_line_variant,
    (live.variant_id IS NOT NULL)               AS line_variant_is_live,
    np.sku IS NOT NULL                          AS is_non_product,
    d.n_variants   AS win_n_variants,
    d.n_targets    AS win_n_targets,
    d.pick_variant_id AS win_variant_id,
    d.pick_target_sku AS win_target_sku,
    d.candidate_variant_ids AS win_candidates,
    lbs.n_variants AS sku_n_variants,
    lbs.n_targets  AS sku_n_targets,
    lbs.pick_variant_id AS sku_variant_id,
    lbs.pick_target_sku AS sku_target_sku,
    lbs.candidate_variant_ids AS sku_candidates,
    bd.tied_variants   AS bd_variants,
    bd.tied_targets    AS bd_targets,
    bd.pick_variant_id AS bd_variant_id,
    bd.pick_target_sku AS bd_target_sku,
    dbs.variant_id AS dead_variant_id
  FROM `biom-reporting-s26.biom_canvas.fct_orders` o
  LEFT JOIN `biom-reporting-s26.biom_canvas.dim_product_variant` live
    ON live.variant_id = o.variant_id AND live.is_current
  LEFT JOIN non_product_sku np ON np.sku = o.sku
  LEFT JOIN dated       d   ON d.order_id = o.order_id AND d.line_item_id = o.line_item_id
  LEFT JOIN live_by_sku lbs ON lbs.sku = o.sku
  LEFT JOIN dead_by_sku dbs ON dbs.sku = o.sku
  LEFT JOIN backdate    bd  ON bd.sku  = o.sku
  WHERE o.is_current
)

SELECT
  order_id,
  line_item_id,
  customer_id,
  order_created_at_utc,
  order_time_sku,
  line_variant_id,
  gross_using_line_price,

  CASE
    WHEN is_non_product                          THEN 'NON_PRODUCT'
    WHEN line_variant_is_live                    THEN 'DIRECT_VARIANT_ID'
    WHEN line_variant_id IS NOT NULL             THEN 'DEAD_VARIANT'
    WHEN win_n_variants >= 1 AND win_n_targets = 1 THEN 'SKU_HISTORY_DATED'
    WHEN win_n_variants  = 1 AND win_n_targets = 0 THEN 'SKU_HISTORY_DATED_NULLSKU'
    WHEN win_n_variants >= 1 AND win_n_targets > 1 THEN 'AMBIGUOUS'
    WHEN sku_n_variants >= 1 AND sku_n_targets = 1 THEN 'SKU_HISTORY_FALLBACK'
    WHEN bd_variants = 1 OR bd_targets = 1         THEN 'SKU_HISTORY_BACKDATED'
    WHEN sku_n_variants >= 1 AND sku_n_targets > 1 THEN 'AMBIGUOUS'
    WHEN dead_variant_id IS NOT NULL             THEN 'DEAD_VARIANT'
    ELSE 'NO_PATH'
  END AS resolution_method,

  CASE
    WHEN is_non_product                          THEN NULL
    WHEN line_variant_is_live                    THEN line_variant_id
    WHEN line_variant_id IS NOT NULL             THEN line_variant_id
    WHEN win_n_variants >= 1 AND win_n_targets = 1 THEN win_variant_id
    WHEN win_n_variants  = 1 AND win_n_targets = 0 THEN win_variant_id
    WHEN win_n_variants >= 1 AND win_n_targets > 1 THEN NULL   -- never guess
    WHEN sku_n_variants >= 1 AND sku_n_targets = 1 THEN sku_variant_id
    WHEN bd_variants = 1 OR bd_targets = 1         THEN bd_variant_id
    WHEN sku_n_variants >= 1 AND sku_n_targets > 1 THEN NULL   -- never guess
    WHEN dead_variant_id IS NOT NULL             THEN dead_variant_id
    ELSE NULL
  END AS resolved_variant_id,

  CASE
    WHEN is_non_product                          THEN NULL
    WHEN line_variant_is_live                    THEN live_sku_of_line_variant
    WHEN line_variant_id IS NOT NULL             THEN NULL
    WHEN win_n_variants >= 1 AND win_n_targets = 1 THEN win_target_sku
    WHEN win_n_variants  = 1 AND win_n_targets = 0 THEN NULL   -- variant is live but unnamed
    WHEN win_n_variants >= 1 AND win_n_targets > 1 THEN NULL
    WHEN sku_n_variants >= 1 AND sku_n_targets = 1 THEN sku_target_sku
    WHEN bd_variants = 1 OR bd_targets = 1         THEN bd_target_sku
    ELSE NULL
  END AS resolved_sku,

  CASE
    WHEN is_non_product THEN NULL
    WHEN win_n_variants >= 1 AND win_n_targets > 1 THEN win_candidates
    WHEN win_n_variants = 0 AND sku_n_variants >= 1 AND sku_n_targets > 1 THEN sku_candidates
    ELSE NULL
  END AS candidate_variant_ids

FROM resolved
```

### `vw_repurchase_base`
Columns: `customer_id`, `first_order_date`, `first_order_month`, `acquisition_channel`, `first_purchase_category`, `first_purchase_sub_category`, `subscription_segment`, `lifetime_orders`, `one_time_orders`, `second_order_date`, `days_first_to_second_order`, `is_repurchaser_2plus`, `is_repurchaser_within_60d`, `is_repurchaser_one_time_2plus`, `lifetime_revenue`, `lifetime_aov`

```sql
WITH orders_dedup AS (
  -- fct_orders is line-grain; order-level columns repeat identically on every line of an order
  -- (verified 0 inconsistencies across the full population) -- collapse to one row per order_id here.
  SELECT
    order_id,
    ANY_VALUE(customer_id) AS customer_id,
    ANY_VALUE(order_created_at_utc) AS order_created_at_utc,
    ANY_VALUE(purchase_type) AS purchase_type,
    ANY_VALUE(order_source) AS order_source
  FROM `biom-reporting-s26.biom_canvas.fct_orders`
  WHERE is_current = TRUE
    AND customer_id IS NOT NULL
  GROUP BY order_id
),

order_seq AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_created_at_utc ASC, order_id ASC) AS order_seq_num
  FROM orders_dedup
),

first_orders AS (
  SELECT
    customer_id,
    order_id AS first_order_id,
    order_created_at_utc AS first_order_at_utc,
    order_source AS first_order_source
  FROM order_seq
  WHERE order_seq_num = 1
),

second_orders AS (
  SELECT customer_id, order_created_at_utc AS second_order_at_utc
  FROM order_seq
  WHERE order_seq_num = 2
),

customer_order_counts AS (
  SELECT
    customer_id,
    COUNT(DISTINCT order_id) AS lifetime_orders,
    COUNT(DISTINCT CASE WHEN purchase_type = 'One Time' THEN order_id END) AS one_time_orders,
    LOGICAL_OR(purchase_type = 'Subscription') AS has_subscription_order
  FROM orders_dedup
  GROUP BY customer_id
),

customer_revenue AS (
  -- gross_using_line_price is line-grain and explicitly SUM-safe per CANVAS Data Dictionary --
  -- NOT the order-level current_total_price column (never SUM that at line grain).
  SELECT
    customer_id,
    SUM(gross_using_line_price) AS lifetime_revenue
  FROM `biom-reporting-s26.biom_canvas.fct_orders`
  WHERE is_current = TRUE
    AND customer_id IS NOT NULL
  GROUP BY customer_id
),

first_order_lines AS (
  SELECT
    fo.customer_id,
    fl.variant_id,
    fl.sku,
    fl.product_title,
    ROW_NUMBER() OVER (
      PARTITION BY fo.customer_id
      ORDER BY fl.gross_using_line_price DESC, fl.line_item_id ASC
    ) AS line_rank
  FROM first_orders fo
  JOIN `biom-reporting-s26.biom_canvas.fct_orders` fl
    ON fl.order_id = fo.first_order_id AND fl.is_current = TRUE
),

first_line AS (
  SELECT * FROM first_order_lines WHERE line_rank = 1
),

-- Existing path: resolve category (and now sub_category) via variant_id -> dim_product (is_current = TRUE)
dim_product_resolved AS (
  SELECT
    fol.customer_id,
    fol.sku,
    fol.product_title,
    dp.product_category AS dp_category,
    dp.product_sub_category AS dp_sub_category
  FROM first_line fol
  LEFT JOIN `biom-reporting-s26.biom_canvas.dim_product` dp
    ON dp.product_key = CAST(fol.variant_id AS STRING) AND dp.is_current = TRUE
),

-- Fallback path: apply the SAME locked SKU-pattern CASE logic dim_product uses
-- (pipelines/canvas/canvas_delta.py get_source_dim_product() categorized CTE)
-- directly against the order line's raw sku/product_title, independent of any join.
-- Updated 2026-07-21: kit wipe-type reclassification (Cleaning/Personal Care) added
-- before the mega-bundle branch, identical logic to canvas_delta.py's dim_product taxonomy.
sku_fallback_classified AS (
  SELECT
    customer_id,
    CASE
      WHEN sku IN ('ONWARDINS01', 'fondue-cashback-sku-1665073709')
        THEN 'Non-product line item'
      WHEN sku LIKE 'P-DIS-%' OR sku LIKE 'P-UDIS-%' OR sku LIKE 'P-6DIS-%' OR sku LIKE 'CSP-DIS-%' OR sku LIKE 'CSP-6DIS-%'
        THEN 'Dispensers'
      WHEN product_title LIKE '%Baby%' AND sku LIKE 'K-DIS-%BAB-%'
        THEN 'Personal Care'
      WHEN sku LIKE 'K-HOL-%'
        THEN 'Other'
      WHEN (sku LIKE 'K-%' AND sku LIKE '%-BAB-%'
            AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
                 OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
                 OR product_title LIKE '%Gift With Purchase%'))
        OR (sku LIKE 'K-%' AND product_title LIKE '%Baby%')
        THEN 'Personal Care'
      WHEN (sku LIKE 'K-%WIP-AP-%' OR sku LIKE 'K-%WIP-ALL-%' OR sku LIKE 'K-%WIP-DSN-%')
        AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
             OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
             OR product_title LIKE '%Gift With Purchase%')
        THEN 'Cleaning'
      WHEN sku LIKE 'K-3ALP-%'
        THEN 'Cleaning'
      WHEN sku = 'K-AP-CUR-PR-TER'
        THEN 'Cleaning'
      WHEN sku = 'K-DSN-PR-TER' OR sku = 'K-DSN-PR-WHI'
        THEN 'Cleaning'
      WHEN sku = 'K-6DIS-WHI-1SAN-STL'
        THEN 'Personal Care'
      WHEN (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
            OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
            OR product_title LIKE '%Gift With Purchase%')
        AND (product_title LIKE '%All-Purpose%'
             OR product_title LIKE '%Cleaning Essentials%'
             OR product_title LIKE '%Disinfecting%')
        AND sku LIKE 'K-%'
        THEN 'Cleaning'
      WHEN (sku LIKE 'K-%WIP-SA-%' OR sku LIKE 'K-%WIP-FLU-%'
        OR sku LIKE 'K-%WIP-BAB-%' OR sku LIKE 'K-%WIP-BOD-%')
        AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
             OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
             OR product_title LIKE '%Gift With Purchase%')
        THEN 'Personal Care'
      WHEN (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
            OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
            OR product_title LIKE '%Gift With Purchase%')
        AND (product_title LIKE '%Sanitizing%'
             OR product_title LIKE '%Flushable%'
             OR product_title LIKE '%Hand Sanitiz%')
        AND sku LIKE 'K-%'
        THEN 'Personal Care'
      WHEN sku LIKE 'K-%DIS-%WIP-%' OR sku LIKE 'K-%DIS-%BAB-%' OR sku LIKE 'K-%DIS-%FLU-%'
        OR sku LIKE 'K-%DIS-%SAN-%' OR sku LIKE 'K-%DIS-%AP-%' OR sku LIKE 'K-%DIS-%DSN-%'
        OR sku LIKE 'K-3ALP-%' OR sku LIKE 'K-1BOD-%' OR sku LIKE 'K-2DIS-%'
        OR sku LIKE 'K-DIS-%' OR sku LIKE 'K-6DIS-%'
        OR sku LIKE 'K-DSN-%'
        OR product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
        OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
        OR product_title LIKE '%Gift With Purchase%'
        THEN 'Other'
      WHEN product_title LIKE '%Holiday%'
        THEN 'Other'
      WHEN sku LIKE '%WIP-AP-%' OR sku LIKE '%WIP-ALL-%' OR sku LIKE '%WIP-6IN-AP-%'
        THEN 'Cleaning'
      WHEN sku LIKE '%WIP-DSN-%' OR sku LIKE '%800WIP-DSN-%'
        THEN 'Cleaning'
      WHEN sku LIKE '%WIP-BAB-%' OR product_title LIKE '%Baby%'
        THEN 'Personal Care'
      WHEN sku LIKE '%WIP-FLU-%'
        THEN 'Personal Care'
      WHEN sku LIKE '%WIP-SAN-%' OR sku LIKE '%WIP-SA-%'
        OR sku LIKE '%WIP-6IN-SAN-%' OR sku LIKE '%WIP-6IN-SA-%'
        THEN 'Personal Care'
      WHEN sku LIKE '%WIP-BOD-%'
        THEN 'Personal Care'
      WHEN sku LIKE 'P-SWG-%'
        THEN 'Other'
      ELSE NULL
    END AS sku_category
  FROM first_line
  WHERE sku IS NOT NULL AND TRIM(sku) != ''
),

-- NEW (2026-07-21): sub_category fallback, structurally identical branch-for-branch to
-- sku_fallback_classified above and to canvas_delta.py's product_sub_category CASE --
-- returns the sub_category value instead of the category value for each branch. The
-- "OR sku IS NULL" legs present in canvas_delta.py are dropped here since this CTE only
-- ever runs on sku IS NOT NULL rows (same WHERE guard as sku_fallback_classified).
sku_fallback_sub_classified AS (
  SELECT
    customer_id,
    CASE
      WHEN sku IN ('ONWARDINS01', 'fondue-cashback-sku-1665073709')
        THEN 'Non-product line item'
      WHEN sku LIKE 'P-6DIS-%' OR sku LIKE 'CSP-6DIS-%'
        THEN 'Mini'
      WHEN sku LIKE 'P-DIS-%' OR sku LIKE 'P-UDIS-%' OR sku LIKE 'CSP-DIS-%'
        THEN 'Full Size'
      WHEN product_title LIKE '%Baby%' AND sku LIKE 'K-DIS-%BAB-%'
        THEN 'Baby'
      WHEN sku LIKE 'K-HOL-%'
        THEN 'Holiday & Seasonal'
      WHEN sku LIKE 'K-%' AND sku LIKE '%-BAB-%'
        AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
             OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
             OR product_title LIKE '%Gift With Purchase%')
        THEN 'Baby'
      WHEN sku LIKE 'K-%' AND product_title LIKE '%Baby%'
        THEN 'Baby'
      WHEN (sku LIKE 'K-%WIP-AP-%' OR sku LIKE 'K-%WIP-ALL-%' OR sku LIKE 'K-%WIP-DSN-%')
        AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
             OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
             OR product_title LIKE '%Gift With Purchase%')
        THEN 'Bundles & Kits'
      WHEN sku LIKE 'K-3ALP-%'
        THEN 'Bundles & Kits'
      WHEN sku = 'K-AP-CUR-PR-TER'
        THEN 'Bundles & Kits'
      WHEN sku = 'K-DSN-PR-TER' OR sku = 'K-DSN-PR-WHI'
        THEN 'Bundles & Kits'
      WHEN sku = 'K-6DIS-WHI-1SAN-STL'
        THEN 'Bundles & Kits'
      WHEN (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
            OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
            OR product_title LIKE '%Gift With Purchase%')
        AND (product_title LIKE '%All-Purpose%'
             OR product_title LIKE '%Cleaning Essentials%'
             OR product_title LIKE '%Disinfecting%')
        AND sku LIKE 'K-%'
        THEN 'Bundles & Kits'
      WHEN (sku LIKE 'K-%WIP-SA-%' OR sku LIKE 'K-%WIP-FLU-%'
            OR sku LIKE 'K-%WIP-BAB-%' OR sku LIKE 'K-%WIP-BOD-%')
        AND (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
             OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
             OR product_title LIKE '%Gift With Purchase%')
        THEN 'Bundles & Kits'
      WHEN (product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
            OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
            OR product_title LIKE '%Gift With Purchase%')
        AND (product_title LIKE '%Sanitizing%'
             OR product_title LIKE '%Flushable%'
             OR product_title LIKE '%Hand Sanitiz%')
        AND sku LIKE 'K-%'
        THEN 'Bundles & Kits'
      WHEN sku LIKE 'K-%DIS-%WIP-%' OR sku LIKE 'K-%DIS-%BAB-%' OR sku LIKE 'K-%DIS-%FLU-%'
        OR sku LIKE 'K-%DIS-%SAN-%' OR sku LIKE 'K-%DIS-%AP-%' OR sku LIKE 'K-%DIS-%DSN-%'
        OR sku LIKE 'K-3ALP-%' OR sku LIKE 'K-1BOD-%' OR sku LIKE 'K-2DIS-%'
        OR sku LIKE 'K-DIS-%' OR sku LIKE 'K-6DIS-%'
        OR sku LIKE 'K-DSN-%'
        OR product_title LIKE '%Starter Kit%' OR product_title LIKE '%Essentials Kit%'
        OR product_title LIKE '%Essentials Refill%' OR product_title LIKE '%Bundle%'
        OR product_title LIKE '%Gift With Purchase%'
        THEN 'Bundles & Kits'
      WHEN sku = 'K-60WIP-2STL-2ALP-2FLU'
        THEN 'Bundles & Kits'
      WHEN product_title LIKE '%Holiday%'
        THEN 'Holiday & Seasonal'
      WHEN sku LIKE '%WIP-AP-%' OR sku LIKE '%WIP-ALL-%' OR sku LIKE '%WIP-6IN-AP-%'
        THEN 'APC'
      WHEN sku LIKE '%WIP-DSN-%' OR sku LIKE '%800WIP-DSN-%'
        THEN 'DSN'
      WHEN sku LIKE '%WIP-BAB-%' OR product_title LIKE '%Baby%'
        THEN 'Baby'
      WHEN sku LIKE '%WIP-FLU-%'
        THEN 'Flushable'
      WHEN sku LIKE '%WIP-SAN-%' OR sku LIKE '%WIP-SA-%'
        OR sku LIKE '%WIP-6IN-SAN-%' OR sku LIKE '%WIP-6IN-SA-%'
        THEN 'Sanitizing'
      WHEN sku LIKE '%WIP-BOD-%'
        THEN 'Body Care'
      WHEN sku LIKE 'P-SWG-%'
        THEN 'Accessories'
      ELSE NULL
    END AS sku_sub_category
  FROM first_line
  WHERE sku IS NOT NULL AND TRIM(sku) != ''
),

first_order_category AS (
  SELECT
    dr.customer_id,
    COALESCE(dr.dp_category, sfc.sku_category, 'Unknown') AS first_purchase_category,
    COALESCE(dr.dp_sub_category, sfsc.sku_sub_category, 'Unknown') AS first_purchase_sub_category
  FROM dim_product_resolved dr
  LEFT JOIN sku_fallback_classified sfc USING (customer_id)
  LEFT JOIN sku_fallback_sub_classified sfsc USING (customer_id)
)

SELECT
  fo.customer_id,
  DATE(fo.first_order_at_utc, 'America/Chicago') AS first_order_date,
  DATE_TRUNC(DATE(fo.first_order_at_utc, 'America/Chicago'), MONTH) AS first_order_month,
  COALESCE(fo.first_order_source, 'unknown') AS acquisition_channel,
  foc.first_purchase_category,
  foc.first_purchase_sub_category,
  CASE WHEN coc.has_subscription_order THEN 'Subscription segment' ELSE 'One Time segment' END AS subscription_segment,
  coc.lifetime_orders,
  coc.one_time_orders,
  DATE(so.second_order_at_utc, 'America/Chicago') AS second_order_date,
  TIMESTAMP_DIFF(so.second_order_at_utc, fo.first_order_at_utc, DAY) AS days_first_to_second_order,
  coc.lifetime_orders >= 2 AS is_repurchaser_2plus,
  (so.second_order_at_utc IS NOT NULL
    AND TIMESTAMP_DIFF(so.second_order_at_utc, fo.first_order_at_utc, DAY) <= 60) AS is_repurchaser_within_60d,
  coc.one_time_orders >= 2 AS is_repurchaser_one_time_2plus,
  cr.lifetime_revenue,
  SAFE_DIVIDE(cr.lifetime_revenue, coc.lifetime_orders) AS lifetime_aov
FROM first_orders fo
JOIN customer_order_counts coc ON coc.customer_id = fo.customer_id
JOIN customer_revenue cr ON cr.customer_id = fo.customer_id
LEFT JOIN second_orders so ON so.customer_id = fo.customer_id
LEFT JOIN first_order_category foc ON foc.customer_id = fo.customer_id
```

### `vw_revenue_subscriptions`
Columns: `record_type`, `revenue_key`, `channel_key`, `revenue_date`, `revenue_month`, `revenue_amount`, `net_revenue_amount`, `discount_amount`, `units_sold`, `purchase_type`, `order_id`, `line_item_id`, `variant_id_str`, `tcin`, `location_id`, `origination_channel`, `reporting_channel`, `fulfillment_type`, `manufacturer_style`, `data_grain`, `order_created_at_utc`, `order_name`, `order_created_datetime_ct`, `order_updated_at_utc`, `fulfillment_status`, `currency_code`, `order_source`, `landing_site`, `referring_site`, `product_id`, `current_sku`, `variant_title`, `quantity`, `unit_price`, `total_discount`, `gross_using_line_price`, `net_line_sales`, `total_shipping`, `customer_state`, `ship_city`, `ship_province`, `ship_zip`, `ship_country_code`, `shipment_status`, `delivery_lifecycle_status`, `delivered_at_utc`, `carrier`, `tracking_number`, `shipment_count`, `financial_status`, `sku`, `product_title`, `product_category`, `product_sub_category`, `customer_id`, `has_loop`, `subscription_id`, `subscription_status`, `subscription_created_at`, `cancelled_at`, `paused_at`, `cancellation_reason`, `billing_interval`, `billing_interval_count`, `total_line_item_discounted_price`, `selling_plan_name`, `selling_plan_group_name`, `is_loop_subscription_certified`, `valid_from`, `valid_to`, `allocated_discount`, `allocated_refund`, `admin_net_revenue`

```sql
WITH revenue AS (
 SELECT
   r.revenue_key,
   r.channel_key,
   r.revenue_date,
   DATE_TRUNC(r.revenue_date, MONTH) AS revenue_month,
   r.revenue_amount,
   r.net_revenue_amount,
   r.discount_amount,
   r.units_sold,
   r.purchase_type,
   r.order_id,
   r.line_item_id,
   r.variant_id,
   r.tcin,
   r.location_id,
   r.origination_channel,
   r.reporting_channel,
   r.fulfillment_type,
   r.manufacturer_style,
   r.data_grain
 FROM `biom-reporting-s26.biom_canvas.fct_revenue` r
 WHERE r.is_current = TRUE
),
-- Product dimension (current only)
products AS (
 SELECT
   shopify_variant_id,
   tcin,
   sku,
   product_title,
   variant_title,
   current_price,
   manufacturer_style,
   is_in_shopify,
   is_in_target,
   product_key,
   product_category,
   product_sub_category
 FROM `biom-reporting-s26.biom_canvas.dim_product`
 WHERE is_current = TRUE
),
-- SKU-based product category fallback for rows where variant_id IS NULL
products_by_sku AS (
  SELECT
    sku,
    product_category,
    product_sub_category,
    product_title
  FROM (
    SELECT
      sku,
      product_category,
      product_sub_category,
      product_title,
      product_key,
      ROW_NUMBER() OVER (
        PARTITION BY sku
        ORDER BY product_key
      ) AS rn
    FROM `biom-reporting-s26.biom_canvas.dim_product`
    WHERE is_current = TRUE
      AND sku IS NOT NULL
      AND sku NOT IN ('1111','2222')
  )
  WHERE rn = 1
),
-- Customer subscription flag
customers AS (
 SELECT
   customer_id,
   has_loop,
   state AS customer_state,
   ship_city,
   ship_province,
   ship_zip,
   ship_country_code
 FROM `biom-reporting-s26.biom_canvas.dim_customer`
 WHERE is_current = TRUE
),
-- Orders for customer join, product fallback, and order timestamp
orders AS (
 SELECT
   order_id,
   line_item_id,
   customer_id,
   sku,
   product_title,
   order_created_at_utc,
   financial_status,
   -- New columns
   order_name,
   order_created_datetime_ct,
   order_updated_at_utc,
   fulfillment_status,
   currency_code,
   order_source,
   landing_site,
   referring_site,
   product_id,
   variant_id,
   current_sku,
   variant_title,
   quantity,
   unit_price,
   total_discount,
   gross_using_line_price,
   net_line_sales,
   total_shipping
 FROM `biom-reporting-s26.biom_canvas.fct_orders`
 WHERE is_current = TRUE
),
delivery_latest AS (
SELECT *
FROM (
SELECT *,
ROW_NUMBER() OVER(
PARTITION BY shopify_order_id
ORDER BY delivered_at_utc DESC NULLS LAST,
shipment_id DESC
) rn
FROM `biom-reporting-s26.biom_canvas.fct_delivery`
WHERE is_current = TRUE
)
WHERE rn = 1
),
-- Proportional discount allocated to each line item
line_discounts AS (
 SELECT
   CAST(order_id AS STRING) AS order_id,
   CAST(line_item_id AS STRING) AS line_item_id,
   SAFE_DIVIDE(
     gross_using_line_price,
     SUM(gross_using_line_price) OVER (PARTITION BY order_id)
   ) * current_total_discounts AS proportional_discount
 FROM `biom-reporting-s26.biom_canvas.fct_orders`
 WHERE is_current = TRUE
),
-- Proportional refund allocated to each line item
order_refunds AS (
 SELECT
   CAST(fo.order_id AS STRING) AS order_id,
   CAST(fo.line_item_id AS STRING) AS line_item_id,
   SAFE_DIVIDE(
     fo.gross_using_line_price,
     SUM(fo.gross_using_line_price) OVER (PARTITION BY fo.order_id)
   ) * COALESCE(ref.total_refunded, 0) AS proportional_refund
 FROM `biom-reporting-s26.biom_canvas.fct_orders` fo
 LEFT JOIN (
   SELECT
     CAST(order_id AS STRING) AS order_id,
     ROUND(SUM(total_refunded), 2) AS total_refunded
   FROM `biom-reporting-s26.biom_canvas.fct_refunds`
   WHERE is_current = TRUE
   GROUP BY 1
 ) ref
   ON CAST(fo.order_id AS STRING) = ref.order_id
 WHERE fo.is_current = TRUE
),
-- Subscriptions (current state)
subscriptions AS (
 SELECT
   subscription_id,
   customer_id,
   status AS subscription_status,
   subscription_created_at,
   cancelled_at,
   paused_at,
   cancellation_reason,
   billing_interval,
   billing_interval_count,
   total_line_item_discounted_price,
   selling_plan_id,
   variant_id AS sub_variant_id,
   origin_order_shopify_id,
   is_loop_subscription_certified,
   valid_from,
   valid_to
 FROM `biom-reporting-s26.biom_canvas.fct_subscriptions`
 WHERE is_current = TRUE
),
-- Subscription plans
plans AS (
 SELECT
   selling_plan_id,
   selling_plan_name,
   selling_plan_group_name
 FROM `biom-reporting-s26.biom_canvas.dim_subscription_plan`
 WHERE is_current = TRUE
),
order_discounts AS (
 SELECT
   CAST(order_id AS STRING) AS order_id,
   MAX(current_total_discounts) AS current_total_discounts
 FROM `biom-reporting-s26.biom_canvas.fct_orders`
 WHERE is_current = TRUE
 GROUP BY 1
)
-- REVENUE LAYER
SELECT
 'revenue' AS record_type,
 r.revenue_key,
 r.channel_key,
 r.revenue_date,
 r.revenue_month,
 r.revenue_amount,
 r.net_revenue_amount,
 r.discount_amount,
 r.units_sold,
 r.purchase_type,
 r.order_id,
 r.line_item_id,
 CAST(r.variant_id AS STRING) AS variant_id_str,
 r.tcin,
 r.location_id,
 r.origination_channel,
 r.reporting_channel,
 r.fulfillment_type,
 r.manufacturer_style,
 r.data_grain,
 o.order_created_at_utc,
 o.order_name,
o.order_created_datetime_ct,
o.order_updated_at_utc,
o.fulfillment_status,
o.currency_code,
o.order_source,
o.landing_site,
o.referring_site,
o.product_id,
o.current_sku,
o.variant_title,
o.quantity,
o.unit_price,
o.total_discount,
o.gross_using_line_price,
o.net_line_sales,
o.total_shipping,
c.customer_state,
c.ship_city,
c.ship_province,
c.ship_zip,
c.ship_country_code,
d.shipment_status,
d.delivery_lifecycle_status,
d.delivered_at_utc,
d.carrier,
d.tracking_number,
d.shipment_count,
 o.financial_status,
 -- Product: variant_id lookup → manufacturer_style → fct_orders fallback
 COALESCE(
   p_shop.sku,
   p_tgt.manufacturer_style,
   o.sku
 ) AS sku,
 COALESCE(
   p_shop.product_title,
   p_tgt.product_title,
   o.product_title
 ) AS product_title,
 -- Category: variant_id lookup → target lookup → SKU-based fallback
 COALESCE(
   p_shop.product_category,
   p_tgt.product_category,
   psku.product_category
 ) AS product_category,
 COALESCE(
   p_shop.product_sub_category,
   p_tgt.product_sub_category,
   psku.product_sub_category
 ) AS product_sub_category,
 -- Customer
 o.customer_id,
 c.has_loop,
 -- Subscription fields NULL for revenue rows
 NULL AS subscription_id,
 NULL AS subscription_status,
 NULL AS subscription_created_at,
 NULL AS cancelled_at,
 NULL AS paused_at,
 NULL AS cancellation_reason,
 NULL AS billing_interval,
 NULL AS billing_interval_count,
 NULL AS total_line_item_discounted_price,
 NULL AS selling_plan_name,
 NULL AS selling_plan_group_name,
 NULL AS is_loop_subscription_certified,
 NULL AS valid_from,
 NULL AS valid_to,
 COALESCE(ld.proportional_discount, 0) AS allocated_discount,
 COALESCE(ref2.proportional_refund, 0) AS allocated_refund,
 CASE
   WHEN r.channel_key = 'shopify'
   THEN r.revenue_amount
     - COALESCE(ld.proportional_discount, 0)
     - COALESCE(ref2.proportional_refund, 0)
   ELSE r.net_revenue_amount
 END AS admin_net_revenue
FROM revenue r
LEFT JOIN products p_shop
 ON CAST(r.variant_id AS STRING) = p_shop.shopify_variant_id
 AND r.channel_key = 'shopify'
LEFT JOIN (
 SELECT * FROM (
   SELECT
     CAST(tcin AS STRING) AS tcin,
     sku,
     product_title,
     manufacturer_style,
     target_item_description,
     is_in_shopify,
     product_category,
     product_sub_category,
     ROW_NUMBER() OVER (
       PARTITION BY tcin
       ORDER BY product_key
     ) AS rn
   FROM `biom-reporting-s26.biom_canvas.dim_product`
   WHERE is_current = TRUE
     AND tcin IS NOT NULL
 )
 WHERE rn = 1
) p_tgt
 ON CAST(r.tcin AS STRING) = p_tgt.tcin
 AND r.channel_key = 'target'
LEFT JOIN orders o
 ON r.order_id = o.order_id
 AND CAST(r.line_item_id AS STRING) = CAST(o.line_item_id AS STRING)
LEFT JOIN customers c
 ON o.customer_id = c.customer_id
 AND o.customer_id IS NOT NULL
LEFT JOIN delivery_latest d
ON CAST(r.order_id AS STRING)=d.shopify_order_id
LEFT JOIN line_discounts ld
 ON CAST(r.order_id AS STRING) = ld.order_id
 AND CAST(r.line_item_id AS STRING) = ld.line_item_id
 AND r.channel_key = 'shopify'
LEFT JOIN order_refunds ref2
 ON CAST(r.order_id AS STRING) = ref2.order_id
 AND CAST(r.line_item_id AS STRING) = ref2.line_item_id
 AND r.channel_key = 'shopify'
LEFT JOIN order_discounts o_disc
 ON CAST(r.order_id AS STRING) = o_disc.order_id
 AND r.channel_key = 'shopify'
LEFT JOIN (
 SELECT
   CAST(order_id AS STRING) AS order_id,
   ROUND(SUM(total_refunded), 2) AS total_refunded
 FROM `biom-reporting-s26.biom_canvas.fct_refunds`
 WHERE is_current = TRUE
 GROUP BY 1
) ref
 ON CAST(r.order_id AS STRING) = ref.order_id
 AND r.channel_key = 'shopify'
LEFT JOIN products_by_sku psku
 ON COALESCE(p_shop.sku, p_tgt.manufacturer_style, o.sku) = psku.sku
 AND p_shop.shopify_variant_id IS NULL
 AND r.channel_key = 'shopify'
WHERE COALESCE(o.sku, '') NOT IN ('1111','2222')
UNION ALL
-- SUBSCRIPTION LAYER
SELECT
 'subscription' AS record_type,
 NULL AS revenue_key,
 'loop' AS channel_key,
 DATE(s.subscription_created_at) AS revenue_date,
 DATE_TRUNC(DATE(s.subscription_created_at), MONTH) AS revenue_month,
 NULL AS revenue_amount,
 NULL AS net_revenue_amount,
 NULL AS discount_amount,
 NULL AS units_sold,
 NULL AS purchase_type,
 NULL AS order_id,
 NULL AS line_item_id,
 NULL AS variant_id_str,
 NULL AS tcin,
 NULL AS location_id,
 NULL AS origination_channel,
 NULL AS reporting_channel,
 NULL AS fulfillment_type,
 NULL AS manufacturer_style,
 NULL AS data_grain,
 NULL AS order_created_at_utc,
 NULL AS financial_status,
 NULL AS order_name,
NULL AS order_created_datetime_ct,
NULL AS order_updated_at_utc,
NULL AS fulfillment_status,
NULL AS currency_code,
NULL AS order_source,
NULL AS landing_site,
NULL AS referring_site,
NULL AS product_id,
NULL AS current_sku,
NULL AS variant_title,
NULL AS quantity,
NULL AS unit_price,
NULL AS total_discount,
NULL AS gross_using_line_price,
NULL AS net_line_sales,
NULL AS total_shipping,
NULL AS customer_state,
NULL AS ship_city,
NULL AS ship_province,
NULL AS ship_zip,
NULL AS ship_country_code,
NULL AS shipment_status,
NULL AS delivery_lifecycle_status,
NULL AS delivered_at_utc,
NULL AS carrier,
NULL AS tracking_number,
NULL AS shipment_count,
 p.sku,
 p.product_title,
 p.product_category,
 p.product_sub_category,
 s.customer_id,
 c.has_loop,
 s.subscription_id,
 s.subscription_status,
 s.subscription_created_at,
 s.cancelled_at,
 s.paused_at,
 s.cancellation_reason,
 s.billing_interval,
 s.billing_interval_count,
 s.total_line_item_discounted_price,
 pl.selling_plan_name,
 pl.selling_plan_group_name,
 s.is_loop_subscription_certified,
 s.valid_from,
 s.valid_to,
 NULL AS allocated_discount,
 NULL AS allocated_refund,
 NULL AS admin_net_revenue
FROM subscriptions s
LEFT JOIN plans pl
 ON s.selling_plan_id = pl.selling_plan_id
LEFT JOIN products p
 ON CAST(s.sub_variant_id AS STRING) = p.shopify_variant_id
LEFT JOIN customers c
 ON s.customer_id = c.customer_id
```

### `vw_shopify_category_geo_detail`
Columns: `order_id`, `line_item_id`, `order_name`, `customer_id`, `order_created_at_utc`, `order_created_datetime_ct`, `order_updated_at_utc`, `financial_status`, `fulfillment_status`, `currency_code`, `order_source`, `landing_site`, `referring_site`, `purchase_type`, `product_id`, `variant_id`, `order_line_sku`, `current_sku`, `order_line_product_title`, `order_line_variant_title`, `product_category`, `product_sub_category`, `dim_product_sku`, `dim_product_title`, `product_type`, `vendor`, `is_bundle`, `is_discontinued`, `quantity`, `unit_price`, `total_discount`, `gross_using_line_price`, `net_line_sales`, `customer_state`, `ship_city`, `ship_province`, `ship_zip`, `ship_country_code`, `shipment_status`, `delivery_lifecycle_status`, `delivered_at_utc`, `carrier`, `tracking_number`, `shipment_count`

```sql
WITH delivery_latest AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY shopify_order_id
      ORDER BY delivered_at_utc DESC NULLS LAST, shipment_id DESC
    ) AS rn
  FROM `biom-reporting-s26.biom_canvas.fct_delivery`
  WHERE is_current = TRUE
)
SELECT
  o.order_id, o.line_item_id, o.order_name, o.customer_id,
  o.order_created_at_utc, o.order_created_datetime_ct, o.order_updated_at_utc,
  o.financial_status, o.fulfillment_status, o.currency_code, o.order_source,
  o.landing_site, o.referring_site, o.purchase_type,
  o.product_id, o.variant_id, o.sku AS order_line_sku, o.current_sku,
  o.product_title AS order_line_product_title, o.variant_title AS order_line_variant_title,
  p.product_category, p.product_sub_category, p.sku AS dim_product_sku,
  p.product_title AS dim_product_title, p.product_type, p.vendor,
  p.is_bundle, p.is_discontinued,
  o.quantity, o.unit_price, o.total_discount, o.gross_using_line_price, o.net_line_sales,
  c.state AS customer_state,
  c.ship_city, c.ship_province, c.ship_zip, c.ship_country_code,
  d.shipment_status, d.delivery_lifecycle_status, d.delivered_at_utc, d.carrier,
  d.tracking_number, d.shipment_count
FROM `biom-reporting-s26.biom_canvas.fct_orders` o
LEFT JOIN `biom-reporting-s26.biom_canvas.dim_product` p
  ON CAST(o.variant_id AS STRING) = p.shopify_variant_id AND p.is_current = TRUE
LEFT JOIN `biom-reporting-s26.biom_canvas.dim_customer` c
  ON o.customer_id = c.customer_id AND c.is_current = TRUE
LEFT JOIN delivery_latest d
  ON CAST(o.order_id AS STRING) = d.shopify_order_id AND d.rn = 1
WHERE o.is_current = TRUE
```

### `vw_shopify_sku_order_financial_detail`
Columns: `order_id`, `order_name`, `order_created_at_utc`, `tags`, `shopify_sku`, `product_category`, `product_sub_category`, `order_line_quantity`, `unit_price`, `line_discount`, `gross_amount`, `net_amount`, `order_total_amount`, `order_total_discount`, `shipment_status`, `delivery_lifecycle_status`, `delivered_at_utc`, `carrier`, `tracking_number`, `tracking_note`

```sql
SELECT
  o.order_id, o.order_name, o.order_created_at_utc,
  rj.live_tags AS tags,
  o.sku AS shopify_sku,
  p.product_category, p.product_sub_category,
  o.quantity AS order_line_quantity,
  o.unit_price,
  o.total_discount AS line_discount,
  o.gross_using_line_price AS gross_amount,
  o.net_line_sales AS net_amount,
  o.current_total_price AS order_total_amount,
  o.current_total_discounts AS order_total_discount,
  d.shipment_status, d.delivery_lifecycle_status, d.delivered_at_utc,
  d.carrier, d.tracking_number,
  CASE WHEN d.shipment_status = "unsupported_carrier_error"
    THEN "Known limitation: Malomo cannot track Amazon Logistics shipments"
    ELSE NULL END AS tracking_note
FROM `biom-reporting-s26.biom_canvas.fct_orders` o
LEFT JOIN (
  SELECT CAST(order_id AS STRING) AS order_id, JSON_VALUE(raw_json, "$.tags") AS live_tags
  FROM `biom-reporting-s26.biom_raw.shopify_orders`
) rj ON o.order_id = rj.order_id
LEFT JOIN `biom-reporting-s26.biom_canvas.dim_product` p
  ON CAST(o.variant_id AS STRING) = p.shopify_variant_id AND p.is_current = TRUE
LEFT JOIN (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY shopify_order_id
    ORDER BY delivered_at_utc DESC NULLS LAST, shipment_id DESC
  ) AS rn
  FROM `biom-reporting-s26.biom_canvas.fct_delivery`
  WHERE is_current = TRUE
) d ON CAST(o.order_id AS STRING) = d.shopify_order_id AND d.rn = 1
WHERE o.is_current = TRUE
```

### `vw_target_week_store`
Columns: `location_id`, `week_start_date`, `location_name`, `location_type`, `store_format`, `store_status`, `city`, `state`, `region`, `district`, `latitude`, `longitude`, `zip_code`, `weekly_pos_amount`, `weekly_pos_units`, `tcins_selling`, `drive_up_amount`, `shipt_app_amount`, `shipt_target_amount`, `alt_fulfillment_pct`, `trailing_4wk_pos_amount`, `trailing_4wk_pos_units`, `wow_pct`

```sql
WITH

-- All active store × week combinations (spine)
store_week_spine AS (
  SELECT DISTINCT
    l.location_id,
    d.week_start_date
  FROM `biom-reporting-s26.biom_canvas.dim_location` l
  CROSS JOIN (
    SELECT DISTINCT week_start_date
    FROM `biom-reporting-s26.biom_canvas.dim_date`
    WHERE week_start_date IN (
      SELECT DISTINCT d2.week_start_date
      FROM `biom-reporting-s26.biom_canvas.fct_target_sales` s2
      JOIN `biom-reporting-s26.biom_canvas.dim_date` d2
        ON s2.sales_date = d2.full_date
      WHERE s2.is_current = TRUE
        AND s2.data_grain = 'daily'
    )
  ) d
  WHERE l.is_current = TRUE
    AND l.location_type IN ('STR', 'STR-V')
    AND l.location_id IN (
      SELECT DISTINCT location_id
      FROM `biom-reporting-s26.biom_canvas.fct_target_sales`
      WHERE is_current = TRUE
    )
),

-- Actual sales (sparse — not every store sells every week)
store_sales_raw AS (
  SELECT
    s.location_id,
    d.week_start_date,
    SUM(s.sale_amount) AS weekly_pos_amount,
    SUM(s.sale_quantity) AS weekly_pos_units,
    COUNT(DISTINCT s.tcin) AS tcins_selling,
    SUM(s.drive_up_sale_a) AS drive_up_amount,
    SUM(s.shipt_app_sale_a) AS shipt_app_amount,
    SUM(s.shipt_target_sale_a) AS shipt_target_amount
  FROM `biom-reporting-s26.biom_canvas.fct_target_sales` s
  JOIN `biom-reporting-s26.biom_canvas.dim_date` d
    ON s.sales_date = d.full_date
  WHERE s.is_current = TRUE
    AND s.data_grain = 'daily'
  GROUP BY 1, 2
),

-- Spine LEFT JOIN sales = zero-filled store × week
store_sales AS (
  SELECT
    sp.location_id,
    sp.week_start_date,
    COALESCE(sr.weekly_pos_amount, 0) AS weekly_pos_amount,
    COALESCE(sr.weekly_pos_units, 0) AS weekly_pos_units,
    COALESCE(sr.tcins_selling, 0) AS tcins_selling,
    COALESCE(sr.drive_up_amount, 0) AS drive_up_amount,
    COALESCE(sr.shipt_app_amount, 0) AS shipt_app_amount,
    COALESCE(sr.shipt_target_amount, 0) AS shipt_target_amount
  FROM store_week_spine sp
  LEFT JOIN store_sales_raw sr
    ON sp.location_id = sr.location_id
    AND sp.week_start_date = sr.week_start_date
),

-- Trailing 4-week store POS
trailing_4wk AS (
  SELECT
    location_id,
    week_start_date,
    SUM(weekly_pos_amount)
      OVER (
        PARTITION BY location_id
        ORDER BY week_start_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
      ) AS trailing_4wk_pos_amount,
    SUM(weekly_pos_units)
      OVER (
        PARTITION BY location_id
        ORDER BY week_start_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
      ) AS trailing_4wk_pos_units
  FROM store_sales
),

-- WoW for store map color
wow AS (
  SELECT
    location_id,
    week_start_date,
    weekly_pos_amount,
    LAG(weekly_pos_amount)
      OVER (
        PARTITION BY location_id
        ORDER BY week_start_date
      ) AS prior_week_pos_amount,
    SAFE_DIVIDE(
      weekly_pos_amount - LAG(weekly_pos_amount)
        OVER (
          PARTITION BY location_id
          ORDER BY week_start_date
        ),
      NULLIF(LAG(weekly_pos_amount)
        OVER (
          PARTITION BY location_id
          ORDER BY week_start_date
        ), 0)
    ) AS wow_pct
  FROM store_sales
)

SELECT
  ss.location_id,
  ss.week_start_date,
  -- Store attributes
  l.location_name,
  l.location_type,
  l.store_format,
  l.store_status,
  l.city,
  l.state,
  l.region,
  l.district,
  l.latitude,
  l.longitude,
  l.zip_code,
  -- Sales metrics
  ss.weekly_pos_amount,
  ss.weekly_pos_units,
  ss.tcins_selling,
  -- Fulfillment mix
  ss.drive_up_amount,
  ss.shipt_app_amount,
  ss.shipt_target_amount,
  SAFE_DIVIDE(
    COALESCE(ss.drive_up_amount, 0)
    + COALESCE(ss.shipt_app_amount, 0)
    + COALESCE(ss.shipt_target_amount, 0),
    NULLIF(ss.weekly_pos_amount, 0)
  ) AS alt_fulfillment_pct,
  -- Trailing metrics
  t4.trailing_4wk_pos_amount,
  t4.trailing_4wk_pos_units,
  -- WoW for map coloring
  w.wow_pct
FROM store_sales ss
JOIN `biom-reporting-s26.biom_canvas.dim_location` l
  ON ss.location_id = l.location_id
  AND l.is_current = TRUE
  AND l.location_type IN ('STR', 'STR-V')
LEFT JOIN trailing_4wk t4
  ON ss.location_id = t4.location_id
  AND ss.week_start_date = t4.week_start_date
LEFT JOIN wow w
  ON ss.location_id = w.location_id
  AND ss.week_start_date = w.week_start_date
```

### `vw_target_week_tcin`
Columns: `tcin`, `week_start_date`, `week_end_date`, `sku`, `product_title`, `manufacturer_style`, `target_item_description`, `product_category`, `product_sub_category`, `is_in_shopify`, `weekly_pos_amount`, `weekly_pos_units`, `stores_selling`, `velocity_units_per_store`, `velocity_units_per_store_4wk`, `avg_weekly_units_4wk`, `drive_up_amount`, `drive_up_units`, `shipt_app_amount`, `shipt_target_amount`, `circular_amount`, `promo_amount`, `regular_amount`, `alt_fulfillment_pct`, `chain_on_hand_q`, `chain_on_purchase_q`, `chain_on_transfer_q`, `stores_with_inventory`, `stores_with_any_record`, `wip_pct`, `wip_pct_store_weighted`, `wip_pct_active`, `weeks_of_supply`, `is_return_week`, `is_return_dominated_4wk`

```sql
WITH

-- One inventory snapshot per tcin per week
-- Use MAX(inventory_date) per week_start_date
-- NEVER SUM across snapshots
inv_weekly AS (
  SELECT
    i.tcin,
    d.week_start_date,
    SUM(i.ending_on_hand_q) AS chain_on_hand_q,
    SUM(i.ending_on_purchase_q) AS chain_on_purchase_q,
    SUM(i.ending_on_transfer_q) AS chain_on_transfer_q,
    COUNT(DISTINCT CASE WHEN i.ending_on_hand_q > 0 THEN i.location_id END) AS stores_with_inventory,
    COUNT(DISTINCT i.location_id) AS stores_with_any_record
  FROM `biom-reporting-s26.biom_canvas.fct_target_inventory` i
  INNER JOIN (
    -- Latest snapshot per tcin per week
    SELECT
      i2.tcin,
      d2.week_start_date,
      MAX(i2.inventory_date) AS latest_snapshot
    FROM `biom-reporting-s26.biom_canvas.fct_target_inventory` i2
    JOIN `biom-reporting-s26.biom_canvas.dim_date` d2
      ON i2.inventory_date = d2.full_date
    WHERE i2.is_current = TRUE
      AND i2.data_grain = 'daily'
    GROUP BY 1, 2
  ) latest
    ON i.tcin = latest.tcin
    AND i.inventory_date = latest.latest_snapshot
  JOIN `biom-reporting-s26.biom_canvas.dim_date` d
    ON i.inventory_date = d.full_date
  WHERE i.is_current = TRUE
    AND i.data_grain = 'daily'
  GROUP BY 1, 2
),

-- Weekly sales aggregated from daily grain
sales_weekly AS (
  SELECT
    s.tcin,
    d.week_start_date,
    SUM(s.sale_amount) AS weekly_pos_amount,
    SUM(s.sale_quantity) AS weekly_pos_units,
    COUNT(DISTINCT s.location_id) AS stores_selling,
    SUM(s.drive_up_sale_a) AS drive_up_amount,
    SUM(s.drive_up_sale_q) AS drive_up_units,
    SUM(s.shipt_app_sale_a) AS shipt_app_amount,
    SUM(s.shipt_target_sale_a) AS shipt_target_amount,
    SUM(s.circular_sale_amount) AS circular_amount,
    SUM(s.promo_sale_amount) AS promo_amount,
    SUM(s.regular_sale_amount) AS regular_amount
  FROM `biom-reporting-s26.biom_canvas.fct_target_sales` s
  JOIN `biom-reporting-s26.biom_canvas.dim_date` d
    ON s.sales_date = d.full_date
  WHERE s.is_current = TRUE
    AND s.data_grain = 'daily'
  GROUP BY 1, 2
),

-- Trailing 4-week velocity per tcin
velocity_4wk AS (
  SELECT
    tcin,
    week_start_date,
    AVG(weekly_pos_units / NULLIF(stores_selling, 0))
      OVER (
        PARTITION BY tcin
        ORDER BY week_start_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
      ) AS velocity_units_per_store_4wk,
    AVG(weekly_pos_units)
      OVER (
        PARTITION BY tcin
        ORDER BY week_start_date
        ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
      ) AS avg_weekly_units_4wk
  FROM sales_weekly
)

SELECT
  COALESCE(sw.tcin, iw.tcin) AS tcin,
  COALESCE(sw.week_start_date, iw.week_start_date) AS week_start_date,
  DATE_ADD(COALESCE(sw.week_start_date, iw.week_start_date), INTERVAL 6 DAY) AS week_end_date,
  -- Product info
  p.sku,
  p.product_title,
  p.manufacturer_style,
  p.target_item_description,
  p.product_category,
  p.product_sub_category,
  p.is_in_shopify,
  -- Sales metrics
  sw.weekly_pos_amount,
  sw.weekly_pos_units,
  sw.stores_selling,
  SAFE_DIVIDE(sw.weekly_pos_units, sw.stores_selling) AS velocity_units_per_store,
  v4.velocity_units_per_store_4wk,
  v4.avg_weekly_units_4wk,
  -- Fulfillment mix
  sw.drive_up_amount,
  sw.drive_up_units,
  sw.shipt_app_amount,
  sw.shipt_target_amount,
  sw.circular_amount,
  sw.promo_amount,
  sw.regular_amount,
  SAFE_DIVIDE(
    COALESCE(sw.drive_up_amount, 0)
    + COALESCE(sw.shipt_app_amount, 0)
    + COALESCE(sw.shipt_target_amount, 0),
    NULLIF(sw.weekly_pos_amount, 0)
  ) AS alt_fulfillment_pct,
  -- Inventory metrics
  iw.chain_on_hand_q,
  iw.chain_on_purchase_q,
  iw.chain_on_transfer_q,
  iw.stores_with_inventory,
  iw.stores_with_any_record,
  -- WIP% (Option A: assortment denominator)
  SAFE_DIVIDE(iw.stores_with_inventory, iw.stores_with_any_record) AS wip_pct,
  SAFE_DIVIDE(
    SUM(iw.stores_with_inventory
      * COALESCE(sw.weekly_pos_units, 0))
      OVER (PARTITION BY sw.week_start_date),
    SUM(iw.stores_with_any_record
      * COALESCE(sw.weekly_pos_units, 0))
      OVER (PARTITION BY sw.week_start_date)
  ) AS wip_pct_store_weighted,
  CASE
    WHEN COALESCE(sw.weekly_pos_units, 0) > 0
    THEN SAFE_DIVIDE(
      iw.stores_with_inventory,
      iw.stores_with_any_record)
    ELSE NULL
  END AS wip_pct_active,
  -- Weeks of supply (NULL when return-dominated)
  CASE
    WHEN v4.avg_weekly_units_4wk <= 0 THEN NULL
    ELSE SAFE_DIVIDE(iw.chain_on_hand_q, v4.avg_weekly_units_4wk)
  END AS weeks_of_supply,
  -- Return flags
  (sw.weekly_pos_amount < 0) AS is_return_week,
  (v4.avg_weekly_units_4wk <= 0) AS is_return_dominated_4wk
FROM sales_weekly sw
FULL OUTER JOIN inv_weekly iw
  ON sw.tcin = iw.tcin
  AND sw.week_start_date = iw.week_start_date
LEFT JOIN velocity_4wk v4
  ON sw.tcin = v4.tcin
  AND sw.week_start_date = v4.week_start_date
LEFT JOIN (
  SELECT * EXCEPT (rn) FROM (
    SELECT
      CAST(tcin AS STRING) AS tcin,
      sku,
      product_title,
      manufacturer_style,
      target_item_description,
      is_in_shopify,
      product_category,
      product_sub_category,
      ROW_NUMBER() OVER (
        PARTITION BY tcin
        ORDER BY product_key
      ) AS rn
    FROM `biom-reporting-s26.biom_canvas.dim_product`
    WHERE is_current = TRUE
      AND tcin IS NOT NULL
  )
  WHERE rn = 1
) p
  ON CAST(COALESCE(sw.tcin, iw.tcin) AS STRING) = p.tcin

```

### `vw_variant_sku_journey`
Columns: `variant_id`, `sku`, `period_start`, `period_end`, `order_lines_in_period`, `sku_period_sequence`, `source`, `exact_date`, `possible_id_reuse`

```sql
WITH ordered AS (
  SELECT
    CAST(variant_id AS STRING)              AS variant_id,
    sku,
    DATE(created_at)                        AS order_date,
    ROW_NUMBER() OVER (
      PARTITION BY variant_id
      ORDER BY created_at
    ) -
    ROW_NUMBER() OVER (
      PARTITION BY variant_id, sku
      ORDER BY created_at
    )                                       AS grp
  FROM `biom-reporting-s26.biom_raw.shopify_order_line_items`
  WHERE variant_id IS NOT NULL
    AND DATE(created_at) < '2026-06-11'
),

order_proxy AS (
  SELECT
    variant_id,
    sku,
    MIN(order_date)                         AS period_start,
    MAX(order_date)                         AS period_end,
    COUNT(*)                                AS order_lines_in_period,
    ROW_NUMBER() OVER (
      PARTITION BY variant_id
      ORDER BY MIN(order_date)
    )                                       AS sku_period_sequence
  FROM ordered
  GROUP BY variant_id, sku, grp
),

scd2_history AS (
  SELECT
    CAST(variant_id AS STRING)              AS variant_id,
    sku,
    DATE(valid_from)                        AS period_start,
    CASE
      WHEN is_current = TRUE THEN NULL
      ELSE DATE(valid_to)
    END                                     AS period_end,
    NULL                                    AS order_lines_in_period,
    ROW_NUMBER() OVER (
      PARTITION BY variant_id
      ORDER BY valid_from
    )                                       AS sku_period_sequence
  FROM `biom-reporting-s26.biom_canvas.dim_product_variant`
)

SELECT
  variant_id,
  sku,
  period_start,
  period_end,
  order_lines_in_period,
  sku_period_sequence,
  'order_proxy'                             AS source,
  FALSE                                     AS exact_date,
  -- possible_id_reuse: ALWAYS FALSE as of 2026-08-06 (KNOWN_GAPS item 14 CLOSED). Was a hardcoded
  -- single-variant_id marker, not a computed heuristic. 56402017714342 and 56402017681574 are both
  -- confirmed NOT ID reuse (one mid-May-2026 bulk re-SKU event; neither product_id ever changed).
  -- Real reuse test = COUNT(DISTINCT product_id) > 1 per variant_id, NOT SKU concurrency.
  -- See scripts/build_sku_change_log.py for the full rationale.
  FALSE                                     AS possible_id_reuse
FROM order_proxy

UNION ALL

SELECT
  variant_id,
  sku,
  period_start,
  period_end,
  order_lines_in_period,
  sku_period_sequence,
  'scd2'                                    AS source,
  TRUE                                      AS exact_date,
  FALSE                                     AS possible_id_reuse
FROM scd2_history

ORDER BY variant_id, period_start, sku_period_sequence
```
