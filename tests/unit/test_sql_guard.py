"""SQL guard unit tests over an isolated fixture catalog (no database needed)."""

from __future__ import annotations

import pytest

from contextforge.config.settings import QueryLimits
from contextforge.security.sql_guard import Catalog, ColumnMeta, SqlRejected, validate_sql

CAT = Catalog(
    schema_name="analytics",
    relations={
        "analytics.orders": {
            "order_id": ColumnMeta("internal"),
            "order_status": ColumnMeta("internal"),
            "customer_unique_key": ColumnMeta("pseudonymous"),
        },
        "analytics.order_items": {"order_id": ColumnMeta("internal"), "price": ColumnMeta("internal")},
    },
    column_types={"analytics.order_items": {"price": "DECIMAL(12,2)"}},
)
LIM = QueryLimits()


def ok(sql: str, max_rows: int = 100):
    return validate_sql(sql, CAT, LIM, max_rows=max_rows)


def rejected(sql: str) -> str:
    with pytest.raises(SqlRejected) as ei:
        ok(sql)
    return ei.value.code


def test_simple_select_is_qualified_and_limited() -> None:
    vq = ok("select order_status, count(*) from orders group by 1")
    assert vq.relations == ("analytics.orders",)
    assert '"analytics"."orders"' in vq.executable_sql
    assert vq.executable_sql.endswith("LIMIT 101")
    assert vq.columns == ("analytics.orders.order_status",)


def test_join_cte_window_and_subquery_allowed() -> None:
    vq = ok(
        "with t as (select order_id, sum(price) s from analytics.order_items group by 1) "
        "select o.order_status, avg(t.s), row_number() over (order by avg(t.s)) "
        "from analytics.orders o join t on t.order_id = o.order_id "
        "where o.order_id in (select order_id from order_items) group by 1"
    )
    assert set(vq.relations) == {"analytics.orders", "analytics.order_items"}
    assert vq.max_sensitivity == "internal"


def test_smaller_user_limit_preserved_and_larger_reduced() -> None:
    assert ok("select order_id from orders limit 5").executable_sql.endswith("LIMIT 5")
    vq = ok("select order_id from orders limit 100000")
    assert vq.executable_sql.endswith("LIMIT 101")
    assert vq.warnings


def test_star_expands_only_to_permitted_columns() -> None:
    vq = ok("select * from orders")
    assert set(vq.columns) == {f"analytics.orders.{c}" for c in CAT.relations["analytics.orders"]}


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DELETE FROM orders", "not_a_query"),
        ("DROP TABLE orders", "not_a_query"),
        ("UPDATE orders SET order_status = 'x'", "not_a_query"),
        ("INSERT INTO orders VALUES (1)", "not_a_query"),
        ("CREATE TABLE x AS SELECT 1", "not_a_query"),
        ("ATTACH 'x.duckdb' AS y", "not_a_query"),
        ("COPY orders TO 'out.csv'", "not_a_query"),
        ("INSTALL httpfs", "not_a_query"),
        ("LOAD httpfs", "not_a_query"),
        ("SET threads = 64", "not_a_query"),
        ("PRAGMA version", "not_a_query"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT * FROM orders; DROP TABLE orders", "multiple_statements"),
        ("SELECT * FROM read_csv('/etc/passwd')", "function_not_allowed"),
        ("SELECT * FROM read_parquet('x.parquet')", "function_not_allowed"),
        ("SELECT * FROM 'data/raw/olist/olist_customers_dataset.csv'", "relation_not_allowed"),
        ("SELECT * FROM glob('*')", "function_not_allowed"),
        ("SELECT read_text('README.md')", "function_not_allowed"),
        ("SELECT getenv('HOME')", "function_not_allowed"),
        ("SELECT current_setting('memory_limit')", "function_not_allowed"),
        ("SELECT * FROM duckdb_settings()", "function_not_allowed"),
        ("SELECT * FROM query_table('orders')", "function_not_allowed"),
        ("SELECT * FROM raw.customers", "relation_not_allowed"),
        ("SELECT * FROM r.raw.customers", "relation_not_allowed"),
        ("SELECT * FROM analytics.secret_table", "relation_not_allowed"),
        ("SELECT * INTO new_t FROM orders", "not_a_query"),
        (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT * FROM r",
            "unsupported_syntax",
        ),
        ("SELECT 1", "relation_not_allowed"),
        ("SELECT list_transform([1], x -> x + 1) FROM orders", "function_not_allowed"),
        ("SELECT * FROM orders USING SAMPLE 10%", "unsupported_syntax"),
        ("SELECT COLUMNS('.*') FROM orders", "function_not_allowed"),
        ("SELECT * FROM orders, unnest([1, 2])", "function_not_allowed"),
        ("SELECT order_id FROM orders LIMIT (SELECT 1)", "unsupported_syntax"),
        ("SUMMARIZE orders", "not_a_query"),
        ("DESCRIBE orders", "not_a_query"),
        ("", "empty"),
        ("SELEC order_id FROM orders", "parse_error"),
    ],
)
def test_dangerous_or_unsupported_sql_rejected(sql: str, code: str) -> None:
    assert rejected(sql) == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_zip_code_prefix FROM orders",
        "SELECT o.secret AS harmless FROM orders o",
        "SELECT string_agg(review_comment_message, ',') FROM orders",
        "SELECT (SELECT max(customer_unique_id) FROM orders) FROM order_items",
        "SELECT order_id FROM orders WHERE order_status IN (SELECT customer_unique_id FROM orders)",
        "WITH x AS (SELECT customer_unique_id AS id FROM orders) SELECT id FROM x",
        "SELECT o.order_id FROM orders o JOIN order_items i ON i.order_id = o.order_id WHERE i.secret_col > 0",
        "SELECT upper(customer_unique_id) || '' FROM orders",
        "SELECT order_id FROM orders ORDER BY customer_unique_id",
        'SELECT "CUSTOMER_UNIQUE_ID" FROM orders',
        "SELECT/**/customer_unique_id/**/FROM orders",
    ],
)
def test_unpermitted_column_leakage_rejected(sql: str) -> None:
    assert rejected(sql) == "column_not_allowed"


def test_complexity_limits() -> None:
    joins = " ".join(f"JOIN orders o{i} ON o{i}.order_id = o.order_id" for i in range(LIM.max_joins + 1))
    assert rejected(f"SELECT o.order_id FROM orders o {joins}") == "too_complex"
    nested = "SELECT order_id FROM orders"
    for _ in range(LIM.max_subquery_depth + 2):
        nested = f"SELECT order_id FROM ({nested}) t"
    assert rejected(nested) == "too_complex"
    with pytest.raises(SqlRejected) as ei:
        validate_sql(
            "SELECT order_id FROM orders WHERE " + " OR ".join(["order_id = 'x'"] * 3000),
            CAT,
            LIM,
            max_rows=10,
        )
    assert ei.value.code in ("too_complex", "too_long")


def test_set_operation_gets_limit() -> None:
    vq = ok("SELECT order_id FROM orders UNION ALL SELECT order_id FROM order_items", max_rows=7)
    assert "LIMIT 8" in vq.executable_sql


def test_current_date_warns_about_historical_data() -> None:
    # Premise changed in Phase 2: the guard runs in every context mode, so its warning is generic;
    # the dataset-specific coverage note is added by the service only in full_context mode
    # (tests/integration/test_context_modes.py).
    vq = ok("SELECT count(*) FROM orders WHERE current_date > DATE '2018-01-01'")
    assert any("historical data" in w and "CURRENT_DATE" in w for w in vq.warnings)
    assert not any("2018" in w or "Olist" in w for w in vq.warnings)


@pytest.mark.parametrize("fn", ["dayofweek", "dayofyear", "dayofmonth"])
def test_allowlisted_day_functions_survive_sqlglot_normalisation(fn: str) -> None:
    # Regression (found by the Phase 2 gold freeze): SQLGlot renames DAYOFWEEK -> DAY_OF_WEEK, so an
    # allow-listed function was rejected. Both spellings must be accepted.
    vq = ok(f"SELECT {fn}(order_id) AS d FROM orders")  # guard is type-agnostic
    assert vq.relations
