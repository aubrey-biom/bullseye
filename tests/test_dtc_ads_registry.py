"""Hermetic guards for the DTC (Shopify) and ads (Meta / Google) registry entries.

The Target entries are guarded by tests/test_audit_drift_guards.py. These are the
rules SPECIFIC to the first non-BPD sources — the ones verified live on
2026-09-10 and written into bq.py as constants — plus the one core change they
needed: `logical_schema()` now injects dependencies, so a composed body
(`depends_on`) can be introspected.

Default tier only: no network, no credentials.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from bpd_mcp import bq
from bpd_mcp import column_roles as cr
from bpd_mcp.bq import (
    DTC_SOURCE_BUCKETS,
    LOGICAL_TABLES,
    BigQueryWarehouse,
    LogicalTable,
    channel_bucket_case,
    is_product_line_expr,
    resolve_references,
)

DTC_TABLES = ("dtc_order_lines", "dtc_refunds", "dtc_revenue_lines", "dtc_customer_first_order")
ADS_FACTS = (
    "ads_meta_daily",
    "ads_google_daily",
    "ads_google_shopping_daily",
    "ads_google_keyword_daily",
    "ads_spend_daily",
)
NEW_TABLES = (*DTC_TABLES, *ADS_FACTS, "ads_campaigns", "media_delivery_status")


# --------------------------------------------------------------------------------------
# The order_source -> bucket map (verified in Shopify admin 2026-09-10)
# --------------------------------------------------------------------------------------


def _bucket_of(source: str) -> str:
    for bucket, sources in DTC_SOURCE_BUCKETS.items():
        if source in sources:
            return bucket
    return "unknown"


@pytest.mark.parametrize(
    ("source", "bucket"),
    [
        ("web", "core_d2c"),
        ("subscription_contract_checkout_one", "core_d2c"),
        ("subscription_contract", "core_d2c"),
        ("3890849", "core_d2c"),  # Shopify's Shop app: real paid orders
        ("242196283393", "gifting"),  # ShopMy Integration: $0 seeding at list value
        ("shopify_draft_order", "manual"),
        ("Direct", "manual"),  # Matrixify imports (BabyCenter Reward Claim)
        ("faire", "wholesale"),
        ("341262598145", "unknown"),  # unidentified, 4 orders / 90d
        ("2329312", "unknown"),
    ],
)
def test_observed_order_sources_land_in_the_intended_bucket(source: str, bucket: str) -> None:
    assert _bucket_of(source) == bucket


def test_no_source_is_in_two_buckets() -> None:
    seen: dict[str, str] = {}
    for bucket, sources in DTC_SOURCE_BUCKETS.items():
        for s in sources:
            assert s not in seen, f"{s!r} is in both {seen[s]!r} and {bucket!r}"
            seen[s] = bucket


def test_the_bucket_case_is_rendered_into_every_classifying_body() -> None:
    """One source of truth: both DTC bodies that carry `channel_bucket` must
    contain the SAME rendered CASE, so the map can never drift between them."""
    rendered = channel_bucket_case("order_source")
    for name in ("dtc_order_lines", "dtc_revenue_lines"):
        assert rendered in LOGICAL_TABLES[name].sql, f"{name} does not embed channel_bucket_case()"
    assert "ELSE 'unknown'" in rendered
    for bucket in DTC_SOURCE_BUCKETS:
        assert f"THEN '{bucket}'" in rendered


def test_product_line_expression_names_every_exclusion() -> None:
    expr = is_product_line_expr()
    for title in bq.DTC_NON_PRODUCT_TITLES:
        assert title in expr
    for pattern in bq.DTC_NON_PRODUCT_TITLE_PATTERNS:
        assert pattern in expr
    for sku in bq.DTC_NON_PRODUCT_SKUS:
        assert f"'{sku}'" in expr
    assert expr in LOGICAL_TABLES["dtc_order_lines"].sql


def test_first_order_is_defined_on_paid_core_d2c_orders_only() -> None:
    """A gifting recipient or a draft order must never mint a 'new customer'."""
    body = LOGICAL_TABLES["dtc_customer_first_order"].sql
    assert "channel_bucket = 'core_d2c'" in body
    assert "is_paid_order" in body
    assert "customer_id IS NOT NULL" in body


# --------------------------------------------------------------------------------------
# Money: NUMERIC sources are cast so the tools' FLOAT64 ratio math holds
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        (
            "dtc_order_lines",
            (
                "gross_using_line_price",
                "net_line_sales",
                "current_total_price",
                "unit_price",
                "total_discount",
            ),
        ),
        ("dtc_refunds", ("total_refunded",)),
        (
            "dtc_revenue_lines",
            ("revenue_amount", "allocated_discount", "allocated_refund", "admin_net_revenue"),
        ),
    ],
)
def test_every_numeric_money_column_is_cast_to_float64(
    table: str, columns: tuple[str, ...]
) -> None:
    body = LOGICAL_TABLES[table].sql
    for col in columns:
        assert re.search(rf"CAST\({col} AS FLOAT64\)", body), (
            f"{table}: {col} is not CAST to FLOAT64"
        )


def test_order_level_totals_are_never_summed_in_the_derived_table() -> None:
    """Rule 2: `order_total` repeats on every line of an order."""
    body = LOGICAL_TABLES["dtc_customer_first_order"].sql
    assert "SUM(order_total)" not in body
    assert "SUM(gross_line)" in body


# --------------------------------------------------------------------------------------
# Ads: uniform vocabulary, STRING campaign ids, campaign fact as the spend source
# --------------------------------------------------------------------------------------


def test_every_ads_fact_shares_the_measure_vocabulary() -> None:
    for name in ADS_FACTS:
        contract = set(LOGICAL_TABLES[name].column_contract)
        assert {"date", "channel", "campaign_id", "spend", "impressions", "clicks"} <= contract, (
            name
        )
        assert {"date", "channel", "campaign", "spend", "impressions", "clicks"} <= set(
            cr.COLUMN_ROLES[name]
        ), name


def test_google_campaign_ids_are_projected_as_strings() -> None:
    """Meta ids are STRING at source; Google's are INT64. Every ads_* body must
    end up STRING so the two channels union and join without casts."""
    for name in ("ads_google_daily", "ads_google_shopping_daily", "ads_google_keyword_daily"):
        assert "CAST(campaign_id AS STRING) AS campaign_id" in LOGICAL_TABLES[name].sql, name
    # Google's dim_campaign branch of the union, too.
    assert (
        "CAST(campaign_id AS STRING) AS campaign_id, 'google'"
        in LOGICAL_TABLES["ads_campaigns"].sql
    )


def test_google_ads_account_id_is_never_called_customer_id() -> None:
    """Google's `customer_id` is the ads ACCOUNT. Projecting it under that name
    would let a `customer_id` role resolve to it and join it to Shopify customers."""
    for name in ("ads_google_daily", "ads_google_shopping_daily", "ads_google_keyword_daily"):
        assert "customer_id AS ads_account_id" in LOGICAL_TABLES[name].sql, name
        assert "customer_id" not in cr.COLUMN_ROLES[name], name


def test_spend_spine_reads_only_the_campaign_fact_on_the_google_side() -> None:
    """Shopping and keyword facts are sub-grains of the campaign fact (together
    ~40% of its spend on 2026-09-10). Unioning them in would double count."""
    deps = set(resolve_references("SELECT * FROM ads_spend_daily", LOGICAL_TABLES))
    assert deps == {"ads_spend_daily", "ads_meta_daily", "ads_google_daily"}


def test_per_row_ratio_columns_are_not_projected() -> None:
    """ctr/cpc/cpm/cpp must be recomputed from sums, never averaged."""
    for name in ADS_FACTS:
        cols = set(_projected(LOGICAL_TABLES[name].sql))
        assert not cols & {"ctr", "cpc", "cpm", "cpp", "average_cpc", "average_cpm"}, (name, cols)


def _projected(sql: str) -> list[str]:
    from tests.test_audit_drift_guards import projected_columns

    return projected_columns(sql)


# --------------------------------------------------------------------------------------
# Registry hygiene specific to the new entries
# --------------------------------------------------------------------------------------


def test_new_entries_read_biom_canvas_only_and_have_no_kiteworks_pattern() -> None:
    for name in NEW_TABLES:
        entry = LOGICAL_TABLES[name]
        assert entry.patterns == (), f"{name} claims a Kiteworks file pattern"
        for fq in entry.base_tables:
            assert fq.split(".")[1] == "biom_canvas", f"{name} reads {fq}"


def test_new_entries_never_list_a_view_as_a_base_table() -> None:
    """`__TABLES__` reports 0 rows for a view, which the health check would read
    as an empty dataset. Views are read in the body and named in the note."""
    for name in NEW_TABLES:
        for fq in LOGICAL_TABLES[name].base_tables:
            assert ".vw_" not in fq, f"{name} lists a view as a base table: {fq}"


def test_new_entries_are_delta_or_restated_feeds() -> None:
    for name in NEW_TABLES:
        assert cr.FEED_KINDS[name] in {"delta_latest_state", "append_restated", "dimensional"}, name


def test_composed_entries_declare_their_dependencies() -> None:
    assert LOGICAL_TABLES["dtc_customer_first_order"].depends_on == ("dtc_order_lines",)
    assert set(LOGICAL_TABLES["ads_spend_daily"].depends_on) == {
        "ads_meta_daily",
        "ads_google_daily",
    }


def test_declared_date_column_is_first_in_every_new_projection() -> None:
    """`heuristic_date_column` picks the FIRST DATE/TIMESTAMP-typed column, and a
    live drift guard asserts it agrees with the declaration. Putting the declared
    column first is what keeps that guard green without an allow-list entry."""
    for name in NEW_TABLES:
        entry = LOGICAL_TABLES[name]
        assert _projected(entry.sql)[0] == entry.date_column, name


# --------------------------------------------------------------------------------------
# logical_schema() injects dependencies (the core change the seam needed)
# --------------------------------------------------------------------------------------


class _RecordingClient:
    """Stands in for bigquery.Client: records the SQL and returns an empty schema."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def query(self, sql: str, job_config: Any = None, location: str | None = None) -> Any:
        self.statements.append(sql)

        class _Job:
            total_bytes_processed = 0
            total_bytes_billed = 0
            cache_hit = False
            job_id = "fake"

            def __init__(self) -> None:
                self.schema: list[Any] = []

            def result(self) -> list[Any]:
                return []

        return _Job()


def test_logical_schema_injects_the_dependencies_of_a_composed_body() -> None:
    reg = {
        "leaf": LogicalTable(
            name="leaf",
            sql="SELECT DATE '2026-01-01' AS d, 1 AS x",
            base_tables=(),
            date_column="d",
        ),
        "composed": LogicalTable(
            name="composed",
            sql="SELECT d, SUM(x) AS x FROM leaf GROUP BY d",
            base_tables=(),
            date_column="d",
            depends_on=("leaf",),
        ),
    }
    client = _RecordingClient()
    wh = BigQueryWarehouse(client=client, registry=reg)
    wh.logical_schema("composed")
    (probe,) = client.statements
    assert probe.lstrip().upper().startswith("WITH LEAF AS (")
    assert "LIMIT 0" in probe


def test_logical_schema_of_a_leaf_body_is_not_wrapped_in_with() -> None:
    reg = {
        "leaf": LogicalTable(
            name="leaf",
            sql="SELECT DATE '2026-01-01' AS d, 1 AS x",
            base_tables=(),
            date_column="d",
        ),
    }
    client = _RecordingClient()
    wh = BigQueryWarehouse(client=client, registry=reg)
    wh.logical_schema("leaf")
    (probe,) = client.statements
    assert not probe.lstrip().upper().startswith("WITH")


def test_the_production_composed_tables_resolve_transitively() -> None:
    assert resolve_references("SELECT 1 FROM dtc_customer_first_order", LOGICAL_TABLES) == {
        "dtc_customer_first_order",
        "dtc_order_lines",
    }
