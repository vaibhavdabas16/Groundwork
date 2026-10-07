"""Integration: core service over real context + real DuckDB (same code path the MCP server uses)."""

from __future__ import annotations

import duckdb
import pytest

from contextforge.service.core import ContextForgeService

pytestmark = pytest.mark.integration


def test_every_metric_sql_executes_through_governed_path(
    service: ContextForgeService, serving_con: duckdb.DuckDBPyConnection
) -> None:
    assert service.store.metrics
    for m in service.store.metrics.values():
        r = service.run_query(m.sql)
        assert r.ok, (m.id, r.error)
        assert r.row_count >= 1
        direct = serving_con.execute(m.sql).fetchall()
        assert [norm(row) for row in direct] == [norm(row) for row in r.rows], m.id


def norm(row) -> tuple:  # type: ignore[no-untyped-def,type-arg]
    """DECIMAL is serialised to float at the JSON boundary; compare numerically to 6 dp."""
    from decimal import Decimal

    return tuple(round(float(v), 6) if isinstance(v, (float, Decimal)) else v for v in row)


def test_metric_values_are_consistent(service: ContextForgeService) -> None:
    v = {
        m: service.run_query(service.store.metrics[m].sql).rows[0][0]
        for m in (
            "metric.order_count",
            "metric.delivered_order_count",
            "metric.distinct_customers",
            "metric.product_sales_value",
            "metric.freight_value_total",
            "metric.items_gross_value",
            "metric.payment_value_total",
            "metric.payment_value_naive_item_join",
        )
    }
    assert v["metric.delivered_order_count"] < v["metric.order_count"]
    assert v["metric.distinct_customers"] < v["metric.order_count"]
    assert v["metric.items_gross_value"] == pytest.approx(
        v["metric.product_sales_value"] + v["metric.freight_value_total"]
    )
    assert (
        v["metric.payment_value_naive_item_join"] > v["metric.payment_value_total"]
    )  # documented anti-pattern


def test_dimension_breakdowns_reconcile_to_total(service: ContextForgeService) -> None:
    total = service.run_query("SELECT sum(price) FROM analytics.order_items").rows[0][0]
    by_cat = service.run_query(
        "SELECT p.product_category_name_english, sum(i.price) FROM analytics.order_items i "
        "LEFT JOIN analytics.products p ON p.product_id = i.product_id GROUP BY 1"
    )
    assert by_cat.ok and not by_cat.truncated
    assert sum(r[1] for r in by_cat.rows) == pytest.approx(total)


def test_get_asset(service: ContextForgeService) -> None:
    r = service.get_asset("analytics.orders")
    assert r.ok and r.asset["id"] == "asset.analytics.orders"
    assert r.certification_status == "draft" and r.queryable
    assert "customer_unique_key" in r.queryable_columns
    assert any("DRAFT" in w for w in r.warnings)
    raw = service.get_asset("asset.raw.customers")
    assert raw.ok and not raw.queryable and "customer_unique_id" in raw.withheld_columns
    missing = service.get_asset("asset.analytics.does_not_exist")
    assert not missing.ok and missing.error.code == "not_found"


def test_get_definition_resolution(service: ContextForgeService) -> None:
    by_id = service.get_definition("metric.on_time_delivery_rate")
    assert by_id.resolution == "metric" and by_id.certification_status == "draft"
    assert any("DRAFT" in w for w in by_id.warnings)
    by_syn = service.get_definition("cancelled orders")
    assert by_syn.resolution == "metric" and by_syn.id == "metric.canceled_order_count"
    rev = service.get_definition("revenue")
    assert rev.resolution == "unsupported_concept" and rev.id == "unsupported.revenue"
    assert {c["id"] for c in rev.candidates} >= {"metric.product_sales_value", "metric.payment_value_total"}
    assert service.get_definition("churn").resolution == "unsupported_concept"
    nf = service.get_definition("blockchain synergy index")
    assert nf.resolution == "not_found" and nf.warnings
    dep = service.get_definition("metric.payment_value_naive_item_join")
    assert dep.certification_status == "deprecated" and any("DEPRECATED" in w for w in dep.warnings)


def test_get_join_path(service: ContextForgeService) -> None:
    r = service.get_join_path("asset.analytics.customers", "asset.analytics.products")
    assert r.ok and r.found
    assert [s.join_id for s in r.steps] == [
        "join.orders__customers",
        "join.orders__order_items",
        "join.order_items__products",
    ]
    assert (
        r.fanout_risk
        and r.steps[1].causes_fanout
        and r.steps[1].bridge == "asset.analytics.order_item_totals"
    )
    safe = service.get_join_path("asset.analytics.order_items", "asset.analytics.products")
    assert safe.found and not safe.fanout_risk
    # The documented join hint is executable
    q = service.run_query(f"SELECT count(*) {safe.sql_join_hint}")
    assert q.ok and q.rows[0][0] == 112_650
    none = service.get_join_path("asset.analytics.geolocation_zip_prefix", "asset.analytics.order_reviews")
    assert none.ok and none.found  # via sellers -> items -> orders -> reviews
    assert none.fanout_risk


def test_get_policy(service: ContextForgeService) -> None:
    p = service.get_policy("asset.analytics.orders:customer_unique_key")
    assert p.ok and p.sensitivity == "pseudonymous" and p.queryable_by_caller
    r = service.get_policy("asset.raw.customers:customer_zip_code_prefix")
    assert r.ok and r.sensitivity == "restricted" and not r.queryable_by_caller
    assert "query.execute" in p.caller_capabilities and p.policy_id


def test_explain_query(service: ContextForgeService) -> None:
    r = service.explain_query("SELECT order_status, count(*) FROM analytics.orders GROUP BY 1")
    assert r.ok and r.valid and r.plan and r.estimated_max_cardinality and r.within_cost_limit
    bad = service.explain_query("SELECT customer_unique_id FROM analytics.orders")
    assert not bad.ok and bad.error.code.startswith("sql_rejected.")


def test_search_via_service_reports_missing_context(service: ContextForgeService) -> None:
    r = service.search_assets("zzqx unknownterm")
    assert r.ok and r.results == [] and r.missing_context_explanation
    r2 = service.search_assets("order", {"types": ["asset"], "layers": ["serving"]}, 3)
    assert r2.ok and all(x.type == "asset" and x.queryable for x in r2.results)
    assert all(x.context_version == service.store.version for x in r2.results)


def test_audit_log_records_calls_without_rows(service: ContextForgeService) -> None:
    service.run_query("SELECT count(*) FROM analytics.orders", {"request_id": "audit-test-1"})
    import sqlite3

    con = sqlite3.connect(service.audit.path)
    rows = con.execute(
        "SELECT tool, status, row_count, executed_sql FROM tool_calls WHERE request_id='audit-test-1'"
    ).fetchall()
    con.close()
    assert (
        rows
        and rows[0][0] == "run_query"
        and rows[0][1] == "ok"
        and rows[0][2] == 1
        and "analytics" in rows[0][3]
    )
