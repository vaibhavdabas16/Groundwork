"""Security boundary tests against the real serving warehouse, through the governed service path.

Where a test needs to bypass the SQL guard (to prove the database layer is an independent defence),
it calls the QueryExecutor directly - this is a controlled fault-injection test, not production use.
"""

from __future__ import annotations

import threading
import time

import duckdb
import pytest

from contextforge.config.settings import Settings
from contextforge.query.executor import ExecutionError, QueryExecutor
from contextforge.service.core import ContextForgeService
from tests.conftest import make_service

pytestmark = [pytest.mark.security, pytest.mark.integration]


def rejected(svc: ContextForgeService, sql: str) -> str:
    r = svc.run_query(sql)
    assert not r.ok, f"query unexpectedly succeeded: {sql}"
    assert r.rows == [] and r.error is not None
    return r.error.code


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM analytics.orders",
        "DROP TABLE analytics.orders",
        "UPDATE analytics.orders SET order_status = 'x'",
        "INSERT INTO analytics.orders SELECT * FROM analytics.orders",
        "CREATE TABLE analytics.x AS SELECT 1",
        "SELECT 1 FROM analytics.orders; DROP TABLE analytics.orders",
        "ATTACH 'data/warehouse/olist_raw.duckdb' AS r",
        "COPY analytics.orders TO 'exfil.csv'",
        "COPY (SELECT * FROM analytics.orders) TO 'exfil.parquet' (FORMAT parquet)",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "PRAGMA database_list",
        "SELECT * FROM read_csv('data/raw/olist/olist_customers_dataset.csv')",
        "SELECT * FROM read_csv_auto('data/raw/olist/olist_order_reviews_dataset.csv')",
        "SELECT * FROM 'data/raw/olist/olist_customers_dataset.csv'",
        "SELECT * FROM read_parquet('https://example.com/x.parquet')",
        "SELECT * FROM glob('data/**')",
        "SELECT content FROM read_text('data/secrets/pseudonym.key')",
        "SELECT getenv('CONTEXTFORGE_PSEUDONYM_KEY')",
        "SELECT current_setting('enable_external_access')",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM duckdb_tables()",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM raw.customers",
        "SELECT * FROM r.raw.order_reviews",
        "CALL pragma_version()",
        "EXPORT DATABASE 'out'",
        "CHECKPOINT",
    ],
)
def test_dangerous_statements_rejected(service: ContextForgeService, sql: str) -> None:
    code = rejected(service, sql)
    assert code.startswith("sql_rejected."), code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_unique_id FROM analytics.customers",
        "SELECT customer_zip_code_prefix FROM analytics.customers",
        "SELECT c.customer_zip_code_prefix AS z FROM analytics.customers c",
        "SELECT review_comment_message FROM analytics.order_reviews",
        "SELECT string_agg(review_comment_message, ' ') FROM analytics.order_reviews",
        "SELECT o.order_id FROM analytics.orders o JOIN analytics.order_reviews r ON r.order_id = o.order_id "
        "WHERE r.review_comment_title IS NOT NULL",
        "SELECT * FROM (SELECT review_comment_title AS t FROM analytics.order_reviews) x",
        "WITH c AS (SELECT customer_unique_id FROM analytics.customers) SELECT * FROM c",
        "SELECT lower(customer_unique_id) FROM analytics.customers",
        "SELECT geolocation_lat FROM analytics.geolocation_zip_prefix",
    ],
)
def test_sensitive_column_leakage_attempts_rejected(service: ContextForgeService, sql: str) -> None:
    assert rejected(service, sql) in ("sql_rejected.column_not_allowed", "sql_rejected.relation_not_allowed")


def test_wildcard_returns_only_approved_columns(service: ContextForgeService) -> None:
    r = service.run_query("SELECT * FROM analytics.customers LIMIT 3")
    assert r.ok
    assert set(r.columns) == {"customer_key", "customer_unique_key", "customer_city", "customer_state"}
    r2 = service.run_query("SELECT * FROM analytics.order_reviews LIMIT 3")
    assert r2.ok and not {"review_comment_title", "review_comment_message"} & set(r2.columns)


def test_raw_sensitive_values_physically_absent_from_serving_db(
    raw_con: duckdb.DuckDBPyConnection, serving_con: duckdb.DuckDBPyConnection
) -> None:
    sample_ids = [
        r[0]
        for r in raw_con.execute(
            "SELECT customer_unique_id FROM raw.customers ORDER BY customer_unique_id LIMIT 200"
        ).fetchall()
    ]
    sample_cids = [
        r[0]
        for r in raw_con.execute(
            "SELECT customer_id FROM raw.customers ORDER BY customer_id LIMIT 200"
        ).fetchall()
    ]
    sample_text = [
        r[0]
        for r in raw_con.execute(
            "SELECT review_comment_message FROM raw.order_reviews WHERE length(review_comment_message) > 40 "
            "ORDER BY review_id LIMIT 50"
        ).fetchall()
    ]
    needles = sample_ids + sample_cids + sample_text
    tables = serving_con.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'analytics' AND data_type = 'VARCHAR'"
    ).fetchall()
    for t, c in tables:
        hits = serving_con.execute(
            f'SELECT count(*) FROM analytics."{t}" WHERE "{c}" IN (SELECT unnest(?::VARCHAR[]))', [needles]
        ).fetchone()[0]
        assert hits == 0, f"raw sensitive value found in analytics.{t}.{c}"
    schemas = {
        r[0]
        for r in serving_con.execute(
            "SELECT DISTINCT schema_name FROM information_schema.schemata"
        ).fetchall()
    }
    assert "raw" not in schemas


def test_oversized_results_are_limited(isolated_settings: Settings) -> None:
    svc = make_service(isolated_settings, "principal.local_analyst", max_rows=25)
    try:
        r = svc.run_query("SELECT order_id FROM analytics.order_items")
        assert r.ok and r.row_count == 25 and r.truncated and r.truncation_reason == "max_rows"
        assert r.max_rows == 25
        r2 = svc.run_query("SELECT order_id FROM analytics.orders LIMIT 1000000")
        assert r2.row_count == 25 and r2.truncated
    finally:
        svc.close()


def test_result_byte_limit(isolated_settings: Settings) -> None:
    s = isolated_settings.model_copy(
        update={"limits": isolated_settings.limits.model_copy(update={"max_result_bytes": 2000})}
    )
    ex = QueryExecutor(s.serving_path, s.limits)
    r = ex.run("SELECT order_id, order_status FROM analytics.orders LIMIT 500", mode="execute", max_rows=500)
    assert r.truncated and r.truncation_reason == "max_result_bytes" and 0 < r.row_count < 500


SLOW_SQL = (
    "SELECT count(*) FROM analytics.order_items a, analytics.order_items b, analytics.orders c "
    "WHERE a.price + b.price > CAST(c.order_purchase_timestamp AS DATE) - DATE '2000-01-01'"
)


def test_long_running_query_is_killed(real_warehouse: Settings) -> None:
    ex = QueryExecutor(
        real_warehouse.serving_path, real_warehouse.limits.model_copy(update={"timeout_seconds": 2.0})
    )
    t0 = time.monotonic()
    with pytest.raises(ExecutionError) as ei:
        ex.run(SLOW_SQL, mode="execute", max_rows=10)
    elapsed = time.monotonic() - t0
    assert ei.value.code == "timeout"
    assert elapsed < 10, elapsed  # hard kill, not a cooperative cancel that lets the query run on


def test_cost_limit_rejects_huge_cross_join(service: ContextForgeService) -> None:
    r = service.run_query(SLOW_SQL)
    assert not r.ok and r.error.code in ("execution.cost_limit_exceeded", "execution.timeout")


def test_concurrency_limit(real_warehouse: Settings) -> None:
    ex = QueryExecutor(
        real_warehouse.serving_path,
        real_warehouse.limits.model_copy(update={"max_concurrent_queries": 1, "timeout_seconds": 4.0}),
    )
    errors: list[str] = []
    t = threading.Thread(target=lambda: _swallow(ex, errors))
    t.start()
    time.sleep(1.0)
    with pytest.raises(ExecutionError) as ei:
        ex.run("SELECT 1 FROM analytics.orders LIMIT 1", mode="execute", max_rows=1)
    assert ei.value.code == "concurrency_limit"
    t.join()
    assert errors == ["timeout"]


def _swallow(ex: QueryExecutor, errors: list[str]) -> None:
    try:
        ex.run(SLOW_SQL, mode="execute", max_rows=1)
    except ExecutionError as e:
        errors.append(e.code)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE analytics.pwned AS SELECT 1 AS x",
        "DELETE FROM analytics.orders",
        "COPY analytics.orders TO 'exfil.csv'",
        "ATTACH 'data/warehouse/olist_raw.duckdb' AS r",
        "SET enable_external_access = true",
        "SET memory_limit = '64GB'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM read_csv('data/raw/olist/olist_customers_dataset.csv')",
        "SELECT * FROM read_text('data/secrets/pseudonym.key')",
    ],
)
def test_database_layer_blocks_even_if_guard_bypassed(real_warehouse: Settings, sql: str) -> None:
    """Fault injection: send unvalidated SQL straight to the worker. The read-only, locked,
    external-access-disabled DuckDB connection must still refuse."""
    ex = QueryExecutor(real_warehouse.serving_path, real_warehouse.limits)
    with pytest.raises(ExecutionError) as ei:
        ex.run(sql, mode="execute", max_rows=5)
    assert ei.value.code in ("execution_error", "worker_error")
    assert "olist_customers_dataset" not in ei.value.message and "pseudonym" not in ei.value.message


def test_serving_file_unchanged_after_attacks(
    real_warehouse: Settings, serving_con: duckdb.DuckDBPyConnection
) -> None:
    assert serving_con.execute("SELECT count(*) FROM analytics.orders").fetchone()[0] == 99_441
    tables = {
        r[0]
        for r in serving_con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='analytics'"
        ).fetchall()
    }
    assert "pwned" not in tables and "x" not in tables


# ---------------- authorization ----------------
def test_unauthorized_tools_rejected(isolated_settings: Settings) -> None:
    meta = make_service(isolated_settings, "principal.local_metadata")
    try:
        r = meta.run_query("SELECT count(*) FROM analytics.orders")
        assert not r.ok and r.error.code == "capability_denied"
        assert not meta.explain_query("SELECT 1 FROM analytics.orders").ok
        assert meta.search_assets("orders").ok
    finally:
        meta.close()


def test_admin_tool_denied_to_agent(service: ContextForgeService) -> None:
    r = service.validate_context()
    assert not r.ok and r.error.code == "capability_denied"


def test_role_cannot_be_self_asserted_via_arguments(service: ContextForgeService) -> None:
    for rc in (
        {"role": "role.context_admin"},
        {"principal": "principal.admin"},
        {"capabilities": ["context.admin"]},
    ):
        r = service.validate_context(rc)
        assert not r.ok and r.error.code == "invalid_request_context"
        assert r.principal == "principal.local_analyst"


def test_verified_only_principal_cannot_use_drafts(isolated_settings: Settings) -> None:
    strict = make_service(isolated_settings, "principal.local_strict")
    try:
        d = strict.get_definition("metric.order_count")
        assert d.resolution == "not_visible" and d.definition is None
        q = strict.run_query("SELECT count(*) FROM analytics.orders")
        assert not q.ok and q.error.code == "sql_rejected.relation_not_allowed"
        s = strict.search_assets("order count")
        assert all(x.certification_status != "draft" for x in s.results)
    finally:
        strict.close()


def test_tool_call_budget_per_request(isolated_settings: Settings) -> None:
    svc = make_service(isolated_settings, "principal.local_analyst", max_tool_calls_per_request=3)
    try:
        rc = {"request_id": "budget-test"}
        assert all(svc.search_assets("orders", request_context=rc).ok for _ in range(3))
        r = svc.search_assets("orders", request_context=rc)
        assert not r.ok and r.error.code == "tool_call_budget_exceeded"
        assert svc.search_assets("orders", request_context={"request_id": "other"}).ok
    finally:
        svc.close()


def test_error_messages_do_not_leak_values(service: ContextForgeService) -> None:
    r = service.run_query("SELECT CAST(order_status AS INTEGER) FROM analytics.orders")
    assert not r.ok
    assert "delivered" not in r.error.message and "canceled" not in r.error.message
