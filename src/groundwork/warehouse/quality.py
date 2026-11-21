"""Executable data-quality assertions and profiling for the raw Olist warehouse.

Two kinds of checks:

* ``invariant``   - must hold; a failure means the build or the source is broken.
* ``observation`` - a known property or violation of the real data. We never manipulate data to
  make these pass; instead the observed value is recorded and compared to the committed baseline
  (``data/manifests/dq_baseline.json``) so an upstream change is detected.

All values reported come from executing SQL against the real warehouse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from groundwork.config.settings import Settings
from groundwork.warehouse.schema import RAW_SCHEMA


@dataclass(frozen=True)
class Check:
    id: str
    kind: Literal["invariant", "observation"]
    description: str
    sql: str  # must return a single scalar
    expect_zero: bool = True  # only meaningful for invariants
    category: str = "general"


def _pk(table: str, cols: str) -> Check:
    return Check(
        f"pk_unique.{table}",
        "invariant",
        f"({cols}) is unique and non-null in raw.{table}",
        f"SELECT (SELECT count(*) FROM raw.{table}) - (SELECT count(DISTINCT ({cols})) FROM raw.{table} "
        f"WHERE ({cols}) IS NOT NULL)",
        category="keys",
    )


def _fk(child: str, col: str, parent: str, pcol: str) -> Check:
    return Check(
        f"fk.{child}.{col}->{parent}",
        "invariant",
        f"every raw.{child}.{col} exists in raw.{parent}.{pcol}",
        f"SELECT count(*) FROM raw.{child} c WHERE c.{col} IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM raw.{parent} p WHERE p.{pcol} = c.{col})",
        category="referential_integrity",
    )


def _obs(id_: str, description: str, sql: str, category: str) -> Check:
    return Check(id_, "observation", description, sql, expect_zero=False, category=category)


CHECKS: list[Check] = [
    _pk("orders", "order_id"),
    _pk("customers", "customer_id"),
    _pk("order_items", "order_id, order_item_id"),
    _pk("order_payments", "order_id, payment_sequential"),
    _pk("order_reviews", "review_id, order_id"),
    _pk("products", "product_id"),
    _pk("sellers", "seller_id"),
    _pk("product_category_name_translation", "product_category_name"),
    _fk("orders", "customer_id", "customers", "customer_id"),
    _fk("order_items", "order_id", "orders", "order_id"),
    _fk("order_items", "product_id", "products", "product_id"),
    _fk("order_items", "seller_id", "sellers", "seller_id"),
    _fk("order_payments", "order_id", "orders", "order_id"),
    _fk("order_reviews", "order_id", "orders", "order_id"),
    Check(
        "grain.customer_id_one_order",
        "invariant",
        "customer_id is per-order: every customers.customer_id appears in exactly one order",
        "SELECT count(*) FROM (SELECT c.customer_id, count(o.order_id) n FROM raw.customers c "
        "LEFT JOIN raw.orders o USING (customer_id) GROUP BY 1) WHERE n <> 1",
        category="grain",
    ),
    Check(
        "range.review_score_1_5",
        "invariant",
        "review_score is between 1 and 5",
        "SELECT count(*) FROM raw.order_reviews WHERE review_score NOT BETWEEN 1 AND 5 OR review_score IS NULL",
        category="validity",
    ),
    Check(
        "range.price_positive",
        "invariant",
        "order_items.price > 0",
        "SELECT count(*) FROM raw.order_items WHERE price IS NULL OR price <= 0",
        category="validity",
    ),
    Check(
        "range.freight_non_negative",
        "invariant",
        "order_items.freight_value >= 0",
        "SELECT count(*) FROM raw.order_items WHERE freight_value IS NULL OR freight_value < 0",
        category="validity",
    ),
    Check(
        "range.payment_value_non_negative",
        "invariant",
        "order_payments.payment_value >= 0",
        "SELECT count(*) FROM raw.order_payments WHERE payment_value IS NULL OR payment_value < 0",
        category="validity",
    ),
    Check(
        "time.purchase_not_null",
        "invariant",
        "every order has a purchase timestamp",
        "SELECT count(*) FROM raw.orders WHERE order_purchase_timestamp IS NULL",
        category="timestamps",
    ),
    Check(
        "time.delivered_after_purchase",
        "invariant",
        "customer delivery is never before purchase",
        "SELECT count(*) FROM raw.orders WHERE order_delivered_customer_date < order_purchase_timestamp",
        category="timestamps",
    ),
    # ---- observations (documented real-data properties / violations) ----
    _obs(
        "dup.review_id_across_orders",
        "review_ids that appear on more than one order",
        "SELECT count(*) FROM (SELECT review_id FROM raw.order_reviews GROUP BY 1 HAVING count(*) > 1)",
        "keys",
    ),
    _obs(
        "grain.orders_with_multiple_reviews",
        "orders with more than one review row",
        "SELECT count(*) FROM (SELECT order_id FROM raw.order_reviews GROUP BY 1 HAVING count(*) > 1)",
        "grain",
    ),
    _obs(
        "grain.orders_with_multiple_payments",
        "orders with more than one payment row",
        "SELECT count(*) FROM (SELECT order_id FROM raw.order_payments GROUP BY 1 HAVING count(*) > 1)",
        "grain",
    ),
    _obs(
        "grain.max_payment_rows_per_order",
        "maximum payment rows for a single order",
        "SELECT max(n) FROM (SELECT count(*) n FROM raw.order_payments GROUP BY order_id)",
        "grain",
    ),
    _obs(
        "grain.orders_with_multiple_items",
        "orders with more than one item row",
        "SELECT count(*) FROM (SELECT order_id FROM raw.order_items GROUP BY 1 HAVING count(*) > 1)",
        "grain",
    ),
    _obs(
        "grain.max_items_per_order",
        "maximum item rows for a single order",
        "SELECT max(n) FROM (SELECT count(*) n FROM raw.order_items GROUP BY order_id)",
        "grain",
    ),
    _obs(
        "grain.orders_with_multiple_sellers",
        "orders whose items come from more than one seller",
        "SELECT count(*) FROM (SELECT order_id FROM raw.order_items GROUP BY 1 "
        "HAVING count(DISTINCT seller_id) > 1)",
        "grain",
    ),
    _obs(
        "grain.customer_unique_id_multiple_customer_ids",
        "customer_unique_ids (people) mapped to more than one customer_id (order-level id)",
        "SELECT count(*) FROM (SELECT customer_unique_id FROM raw.customers GROUP BY 1 HAVING count(*) > 1)",
        "grain",
    ),
    _obs(
        "grain.distinct_customer_unique_id",
        "distinct customer_unique_id values",
        "SELECT count(DISTINCT customer_unique_id) FROM raw.customers",
        "grain",
    ),
    _obs(
        "grain.geolocation_rows_per_zip_prefix_max",
        "max geolocation rows sharing one ZIP prefix",
        "SELECT max(n) FROM (SELECT count(*) n FROM raw.geolocation GROUP BY geolocation_zip_code_prefix)",
        "grain",
    ),
    _obs(
        "grain.geolocation_distinct_zip_prefix",
        "distinct ZIP prefixes in geolocation",
        "SELECT count(DISTINCT geolocation_zip_code_prefix) FROM raw.geolocation",
        "grain",
    ),
    _obs(
        "grain.geolocation_zip_prefix_multiple_states",
        "ZIP prefixes mapped to more than one state",
        "SELECT count(*) FROM (SELECT geolocation_zip_code_prefix FROM raw.geolocation GROUP BY 1 "
        "HAVING count(DISTINCT geolocation_state) > 1)",
        "grain",
    ),
    _obs(
        "ri.orders_without_items",
        "orders with no order_items rows",
        "SELECT count(*) FROM raw.orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM raw.order_items i WHERE i.order_id = o.order_id)",
        "referential_integrity",
    ),
    _obs(
        "ri.orders_without_payments",
        "orders with no payment rows",
        "SELECT count(*) FROM raw.orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM raw.order_payments p WHERE p.order_id = o.order_id)",
        "referential_integrity",
    ),
    _obs(
        "ri.orders_without_reviews",
        "orders with no review rows",
        "SELECT count(*) FROM raw.orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM raw.order_reviews r WHERE r.order_id = o.order_id)",
        "referential_integrity",
    ),
    _obs(
        "ri.customer_zip_not_in_geolocation",
        "customers whose ZIP prefix is absent from geolocation",
        "SELECT count(*) FROM raw.customers c WHERE NOT EXISTS (SELECT 1 FROM raw.geolocation g "
        "WHERE g.geolocation_zip_code_prefix = c.customer_zip_code_prefix)",
        "referential_integrity",
    ),
    _obs(
        "ri.seller_zip_not_in_geolocation",
        "sellers whose ZIP prefix is absent from geolocation",
        "SELECT count(*) FROM raw.sellers s WHERE NOT EXISTS (SELECT 1 FROM raw.geolocation g "
        "WHERE g.geolocation_zip_code_prefix = s.seller_zip_code_prefix)",
        "referential_integrity",
    ),
    _obs(
        "ri.product_category_missing_translation",
        "distinct non-null categories without English translation",
        "SELECT count(DISTINCT product_category_name) FROM raw.products p WHERE product_category_name IS NOT "
        "NULL AND NOT EXISTS (SELECT 1 FROM raw.product_category_name_translation t "
        "WHERE t.product_category_name = p.product_category_name)",
        "referential_integrity",
    ),
    _obs(
        "null.product_category",
        "products with NULL category",
        "SELECT count(*) FROM raw.products WHERE product_category_name IS NULL",
        "missing_values",
    ),
    _obs(
        "consistency.payment_vs_items_mismatch_orders",
        "orders (with both items and payments) where sum(payment_value) differs from sum(price+freight) "
        "by more than 0.01",
        "SELECT count(*) FROM (SELECT order_id, sum(payment_value) p FROM raw.order_payments GROUP BY 1) a "
        "JOIN (SELECT order_id, sum(price + freight_value) i FROM raw.order_items GROUP BY 1) b "
        "USING (order_id) WHERE abs(p - i) > 0.01",
        "consistency",
    ),
    _obs(
        "consistency.zero_value_payments",
        "payment rows with payment_value = 0",
        "SELECT count(*) FROM raw.order_payments WHERE payment_value = 0",
        "consistency",
    ),
    _obs(
        "consistency.payment_type_not_defined",
        "payment rows with payment_type = 'not_defined'",
        "SELECT count(*) FROM raw.order_payments WHERE payment_type = 'not_defined'",
        "consistency",
    ),
    _obs(
        "consistency.zero_installments",
        "payment rows with payment_installments = 0",
        "SELECT count(*) FROM raw.order_payments WHERE payment_installments = 0",
        "consistency",
    ),
    _obs(
        "status.delivered_without_delivery_date",
        "status 'delivered' but no customer delivery date",
        "SELECT count(*) FROM raw.orders WHERE order_status = 'delivered' "
        "AND order_delivered_customer_date IS NULL",
        "timestamps",
    ),
    _obs(
        "status.not_delivered_with_delivery_date",
        "status other than 'delivered' but has a delivery date",
        "SELECT count(*) FROM raw.orders WHERE order_status <> 'delivered' "
        "AND order_delivered_customer_date IS NOT NULL",
        "timestamps",
    ),
    _obs(
        "time.carrier_before_purchase",
        "carrier handoff timestamp earlier than purchase timestamp",
        "SELECT count(*) FROM raw.orders WHERE order_delivered_carrier_date < order_purchase_timestamp",
        "timestamps",
    ),
    _obs(
        "time.carrier_before_approval",
        "carrier handoff timestamp earlier than approval timestamp",
        "SELECT count(*) FROM raw.orders WHERE order_delivered_carrier_date < order_approved_at",
        "timestamps",
    ),
    _obs(
        "time.delivered_before_carrier",
        "customer delivery earlier than carrier handoff",
        "SELECT count(*) FROM raw.orders WHERE order_delivered_customer_date < order_delivered_carrier_date",
        "timestamps",
    ),
    _obs(
        "time.max_purchase_date_epoch",
        "latest purchase timestamp (epoch seconds) - the dataset 'as-of' date",
        "SELECT epoch(max(order_purchase_timestamp))::BIGINT FROM raw.orders",
        "timestamps",
    ),
    _obs(
        "outlier.items_price_above_p99_9",
        "item rows with price above the 99.9th percentile",
        "SELECT count(*) FROM raw.order_items WHERE price > (SELECT quantile_cont(price, 0.999) "
        "FROM raw.order_items)",
        "outliers",
    ),
    _obs(
        "outlier.freight_exceeds_price",
        "item rows where freight_value exceeds price",
        "SELECT count(*) FROM raw.order_items WHERE freight_value > price",
        "outliers",
    ),
    _obs(
        "fanout.naive_payment_item_review_join_inflation_cents",
        "inflation (in cents) of sum(payment_value) when payments are naively joined to items and reviews",
        "SELECT CAST(round(((SELECT sum(p.payment_value) FROM raw.order_payments p "
        "JOIN raw.order_items i USING (order_id) LEFT JOIN raw.order_reviews r USING (order_id)) - "
        "(SELECT sum(payment_value) FROM raw.order_payments)) * 100) AS BIGINT)",
        "fanout",
    ),
]


def run_checks(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    results = []
    for chk in CHECKS:
        row = con.execute(chk.sql).fetchone()
        value = row[0] if row else None
        if isinstance(value, float):
            value = round(value, 6)
        passed: bool | None = (value == 0) if chk.kind == "invariant" else None
        results.append(
            {
                "id": chk.id,
                "kind": chk.kind,
                "category": chk.category,
                "description": chk.description,
                "value": value,
                "passed": passed,
            }
        )
    return results


def profile(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Per-table row counts, per-column null percentages and distinct counts, and date ranges."""
    out: dict[str, Any] = {}
    for table, cols in RAW_SCHEMA.items():
        total_row = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
        assert total_row is not None
        total = total_row[0]
        columns: dict[str, Any] = {}
        for c, t in cols.items():
            row = con.execute(
                f'SELECT count(*) - count("{c}"), approx_count_distinct("{c}"), '
                f'min("{c}")::VARCHAR, max("{c}")::VARCHAR FROM raw.{table}'
            ).fetchone()
            assert row is not None
            nulls, distinct, mn, mx = row
            info: dict[str, Any] = {
                "type": t,
                "nulls": nulls,
                "null_pct": round(100.0 * nulls / total, 3) if total else 0.0,
                "approx_distinct": distinct,
            }
            if t in ("TIMESTAMP", "INTEGER") or t.startswith("DECIMAL") or t == "DOUBLE":
                info["min"], info["max"] = mn, mx
            columns[c] = info
        out[table] = {"rows": total, "columns": columns}
    return out


def baseline_path(settings: Settings) -> Path:
    return settings.manifest_dir / "dq_baseline.json"


def run_quality(settings: Settings, *, write_baseline_if_missing: bool = True) -> dict[str, Any]:
    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    try:
        results = run_checks(con)
        prof = profile(con)
    finally:
        con.close()
    observed = {r["id"]: r["value"] for r in results if r["kind"] == "observation"}
    bp = baseline_path(settings)
    baseline_drift: dict[str, Any] = {}
    if bp.is_file():
        baseline = json.loads(bp.read_text(encoding="utf-8"))["observations"]
        for k, v in observed.items():
            if k in baseline and baseline[k] != v:
                baseline_drift[k] = {"baseline": baseline[k], "observed": v}
    elif write_baseline_if_missing:
        bp.write_text(
            json.dumps(
                {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "observations": observed},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "invariants_failed": [r["id"] for r in results if r["kind"] == "invariant" and not r["passed"]],
        "observation_baseline_drift": baseline_drift,
        "checks": results,
        "profile": prof,
    }
    (settings.manifest_dir / "dq_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report
