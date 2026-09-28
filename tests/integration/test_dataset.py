"""Integration: real Olist data - provenance, integrity, types, grain, fanout, repeatability."""

from __future__ import annotations

import csv
import json
from decimal import Decimal

import duckdb
import pytest

from contextforge.common.hashing import sha256_file
from contextforge.config.settings import Settings
from contextforge.ingestion.acquire import EXPECTED_FILES, load_manifest, raw_file, verify_raw_files
from contextforge.warehouse.build import SERVING_SQL, build, build_manifest_path
from contextforge.warehouse.quality import run_checks
from contextforge.warehouse.schema import RAW_SCHEMA

pytestmark = pytest.mark.integration

# Row counts of the published Olist snapshot (Kaggle dataset version recorded in the manifest).
EXPECTED_ROWS = {
    "customers": 99_441,
    "geolocation": 1_000_163,
    "order_items": 112_650,
    "order_payments": 103_886,
    "order_reviews": 99_224,
    "orders": 99_441,
    "products": 32_951,
    "sellers": 3_095,
    "product_category_name_translation": 71,
}


def test_manifest_records_provenance(real_warehouse: Settings) -> None:
    m = load_manifest(real_warehouse)
    assert m is not None
    assert m["dataset_ref"] == "olistbr/brazilian-ecommerce"
    assert m["license"] == "CC BY-NC-SA 4.0"
    assert m["retrieved_at"] and m["acquisition_mode"] in ("kaggle", "zip", "dir")
    assert {f["file"] for f in m["files"]} == set(EXPECTED_FILES)
    assert all(len(f["sha256"]) == 64 for f in m["files"])


def test_raw_files_match_recorded_checksums(real_warehouse: Settings) -> None:
    assert verify_raw_files(real_warehouse) == []


def test_row_counts_match_source_csv_and_warehouse(
    real_warehouse: Settings, raw_con: duckdb.DuckDBPyConnection
) -> None:
    for table, expected in EXPECTED_ROWS.items():
        with raw_file(real_warehouse, table).open(encoding="utf-8", newline="") as fh:
            csv_rows = sum(1 for _ in csv.reader(fh)) - 1  # independent of DuckDB's reader
        db_rows = raw_con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
        assert csv_rows == db_rows == expected, table


def test_build_manifest_counts_match_db(
    real_warehouse: Settings, serving_con: duckdb.DuckDBPyConnection
) -> None:
    bm = json.loads(build_manifest_path(real_warehouse).read_text())
    assert bm["raw_row_counts"] == EXPECTED_ROWS
    for t, n in bm["serving_row_counts"].items():
        assert serving_con.execute(f"SELECT count(*) FROM analytics.{t}").fetchone()[0] == n


def test_raw_types_are_explicit(raw_con: duckdb.DuckDBPyConnection) -> None:
    for table, cols in RAW_SCHEMA.items():
        got = {r[0]: r[1] for r in raw_con.execute(f"DESCRIBE raw.{table}").fetchall()}
        assert got == cols, table
    # leading zeros preserved in ZIP prefixes
    assert (
        raw_con.execute(
            "SELECT count(*) FROM raw.customers WHERE customer_zip_code_prefix LIKE '0%'"
        ).fetchone()[0]
        > 0
    )


def test_all_invariants_hold_on_real_data(raw_con: duckdb.DuckDBPyConnection) -> None:
    failed = [r for r in run_checks(raw_con) if r["kind"] == "invariant" and not r["passed"]]
    assert failed == []


def test_observations_match_committed_baseline(
    real_warehouse: Settings, raw_con: duckdb.DuckDBPyConnection
) -> None:
    baseline = json.loads((real_warehouse.manifest_dir / "dq_baseline.json").read_text())["observations"]
    observed = {r["id"]: r["value"] for r in run_checks(raw_con) if r["kind"] == "observation"}
    assert observed == baseline


def test_grain_facts(raw_con: duckdb.DuckDBPyConnection) -> None:
    q = lambda s: raw_con.execute(s).fetchone()[0]  # noqa: E731
    assert q("SELECT count(DISTINCT customer_unique_id) FROM raw.customers") == 96_096
    assert q("SELECT count(DISTINCT review_id) FROM raw.order_reviews") < EXPECTED_ROWS["order_reviews"]
    assert (
        q("SELECT count(DISTINCT geolocation_zip_code_prefix) FROM raw.geolocation")
        < EXPECTED_ROWS["geolocation"]
    )
    assert q("SELECT max(n) FROM (SELECT count(*) n FROM raw.order_payments GROUP BY order_id)") > 1


def test_naive_join_multiplies_revenue_and_bridges_do_not(serving_con: duckdb.DuckDBPyConnection) -> None:
    q = lambda s: serving_con.execute(s).fetchone()[0]  # noqa: E731
    correct = q("SELECT sum(payment_value) FROM analytics.order_payments")
    naive = q(
        "SELECT sum(p.payment_value) FROM analytics.order_payments p "
        "JOIN analytics.order_items i USING (order_id) LEFT JOIN analytics.order_reviews r USING (order_id)"
    )
    assert naive > correct * Decimal("1.2")
    via_bridges = q(
        "SELECT sum(pt.payment_total) FROM analytics.orders o "
        "LEFT JOIN analytics.order_item_totals it USING (order_id) "
        "LEFT JOIN analytics.order_payment_totals pt USING (order_id) "
        "LEFT JOIN analytics.order_review_summary rs USING (order_id)"
    )
    assert via_bridges == correct
    assert q("SELECT sum(items_price_total) FROM analytics.order_item_totals") == q(
        "SELECT sum(price) FROM analytics.order_items"
    )


def test_serving_preserves_grain_of_source(
    serving_con: duckdb.DuckDBPyConnection, raw_con: duckdb.DuckDBPyConnection
) -> None:
    for t in ("orders", "customers", "order_items", "order_payments", "order_reviews", "products", "sellers"):
        assert serving_con.execute(f"SELECT count(*) FROM analytics.{t}").fetchone()[0] == EXPECTED_ROWS[t]
    assert (
        serving_con.execute("SELECT count(DISTINCT customer_unique_key) FROM analytics.orders").fetchone()[0]
        == 96_096
    )


def test_serving_schema_matches_context(serving_con: duckdb.DuckDBPyConnection) -> None:
    from contextforge.context.loader import load_context

    store = load_context(__import__("pathlib").Path(__file__).resolve().parents[2] / "context")
    db = {
        t: {r[0]: r[1] for r in serving_con.execute(f"DESCRIBE analytics.{t}").fetchall()}
        for (t,) in serving_con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics'"
        ).fetchall()
    }
    serving_assets = {a.table: a for a in store.assets.values() if a.layer == "serving"}
    assert set(serving_assets) == set(db) == set(SERVING_SQL)
    for t, a in serving_assets.items():
        assert {c.name: c.data_type for c in a.columns} == db[t], t


def test_ingestion_is_repeatable(
    real_warehouse: Settings, tmp_path, serving_con: duckdb.DuckDBPyConnection
) -> None:
    """Rebuild from the same source files into a fresh location: identical counts and content."""
    s2 = real_warehouse.model_copy(
        update={
            "warehouse_path": tmp_path / "raw.duckdb",
            "serving_path": tmp_path / "serving.duckdb",
            "manifest_dir": tmp_path / "manifests",
            "data_dir": real_warehouse.data_dir,
        }
    )
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "olist_source_manifest.json").write_text(
        (real_warehouse.manifest_dir / "olist_source_manifest.json").read_text()
    )
    m = build(s2)
    assert m["raw_row_counts"] == EXPECTED_ROWS
    con2 = duckdb.connect(str(s2.serving_path), read_only=True)
    try:
        for t in SERVING_SQL:
            h = "SELECT count(*), sum(hash(columns(*))) FROM analytics.{t}"
            a = serving_con.execute(h.format(t=t)).fetchall()
            b = con2.execute(h.format(t=t)).fetchall()
            assert a == b, t  # same key => identical pseudonyms and identical content
    finally:
        con2.close()
    assert sha256_file(raw_file(real_warehouse, "orders")) == next(
        f["sha256"] for f in load_manifest(real_warehouse)["files"] if f["table"] == "orders"
    )
