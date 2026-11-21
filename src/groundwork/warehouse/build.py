"""Build the raw ingestion warehouse and the separate agent-facing serving warehouse.

Two physically separate DuckDB files:

* ``olist_raw.duckdb``     - schema ``raw``: every original column, explicitly typed. Used only by
  ingestion, profiling and data-quality checks. Never opened by the MCP server.
* ``olist_serving.duckdb`` - schema ``analytics``: approved relations only. Sensitive raw fields are
  either excluded or replaced by keyed pseudonyms (HMAC-SHA256) at build time. This is the only file
  the query worker opens, and it opens it read-only.

Transformations from raw -> analytics are documented inline and in docs/data_quality_report.md.
No records are dropped, invented, or "cleaned" in raw. Order-grain helper tables are pre-aggregated
to prevent join fanout, but the item/payment/review detail tables are preserved at source grain.
"""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import duckdb

from groundwork.config.settings import Settings
from groundwork.ingestion.acquire import load_manifest, raw_file, verify_raw_files
from groundwork.observability.logging import get_logger
from groundwork.warehouse.schema import RAW_SCHEMA

log = get_logger("warehouse.build")


class BuildError(RuntimeError):
    pass


def build_manifest_path(settings: Settings) -> Path:
    return settings.manifest_dir / "warehouse_build_manifest.json"


def _pseudonym_key(settings: Settings) -> bytes:
    if settings.pseudonym_key_env:
        return settings.pseudonym_key_env.encode("utf-8")
    key_file = settings.secrets_dir / "pseudonym.key"
    if not key_file.is_file():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
        log.info("generated new local pseudonymization key", extra={"path": str(key_file)})
    return key_file.read_text(encoding="utf-8").strip().encode("utf-8")


def _load_raw(con: duckdb.DuckDBPyConnection, settings: Settings) -> dict[str, dict[str, Any]]:
    """Load each CSV as all-VARCHAR, then cast explicitly, counting any cast failures."""
    stats: dict[str, dict[str, Any]] = {}
    # Qualify with the catalog name: a database file named e.g. raw.duckdb would otherwise make
    # "raw.<table>" ambiguous between catalog "raw" and schema "raw".
    cat_row = con.execute("SELECT current_database()").fetchone()
    assert cat_row is not None
    cat = cat_row[0]
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{cat}".raw')
    for table, cols in RAW_SCHEMA.items():
        path = raw_file(settings, table).as_posix()
        con.execute(
            "CREATE OR REPLACE TEMP TABLE stage AS SELECT * FROM read_csv(?, header=true, "
            "all_varchar=true, strict_mode=true)",
            [path],
        )
        stage_cols = [r[0] for r in con.execute("DESCRIBE stage").fetchall()]
        if stage_cols != list(cols):
            raise BuildError(f"{table}: source columns {stage_cols} != expected {list(cols)}")
        select = ", ".join(f'TRY_CAST(NULLIF("{c}", \'\') AS {t}) AS "{c}"' for c, t in cols.items())
        con.execute(f'CREATE OR REPLACE TABLE "{cat}".raw.{table} AS SELECT {select} FROM stage')
        # Cast failures: non-empty source value that became NULL after the typed cast.
        failures = {}
        for c, t in cols.items():
            if t == "VARCHAR":
                continue
            n = con.execute(
                f"SELECT count(*) FROM stage WHERE NULLIF(\"{c}\", '') IS NOT NULL "
                f'AND TRY_CAST("{c}" AS {t}) IS NULL'
            ).fetchone()
            assert n is not None
            # Precision loss for decimals: compare to DOUBLE parse.
            if t.startswith("DECIMAL"):
                lost = con.execute(
                    f'SELECT count(*) FROM stage WHERE abs(TRY_CAST("{c}" AS DOUBLE) - '
                    f'CAST(TRY_CAST("{c}" AS {t}) AS DOUBLE)) > 1e-9'
                ).fetchone()
                assert lost is not None
                if lost[0]:
                    failures[f"{c}:precision_loss"] = lost[0]
            if n[0]:
                failures[c] = n[0]
        if failures:
            raise BuildError(f"{table}: type cast failures {failures}")
        rows = con.execute(f'SELECT count(*) FROM "{cat}".raw.{table}').fetchone()
        assert rows is not None
        stats[table] = {"rows": rows[0], "columns": cols}
        log.info("loaded raw table", extra={"table": table, "rows": rows[0]})
    con.execute("DROP TABLE IF EXISTS stage")
    return stats


# Each serving relation: name -> (grain description, SQL over the attached raw database ``r``).
# ``cf_pseudonym(x)`` is a build-time-only SQL macro that looks up an HMAC-SHA256 pseudonym
# (computed in Python, never in SQL text) from a temporary mapping table.
SERVING_SQL: dict[str, str] = {
    "orders": """
        SELECT o.order_id,
               cf_pseudonym(o.customer_id)          AS customer_key,
               cf_pseudonym(c.customer_unique_id)   AS customer_unique_key,
               o.order_status,
               o.order_purchase_timestamp,
               o.order_approved_at,
               o.order_delivered_carrier_date,
               o.order_delivered_customer_date,
               o.order_estimated_delivery_date
        FROM r.raw.orders o LEFT JOIN r.raw.customers c USING (customer_id)
    """,
    "customers": """
        SELECT cf_pseudonym(customer_id)        AS customer_key,
               cf_pseudonym(customer_unique_id) AS customer_unique_key,
               customer_city,
               customer_state
        FROM r.raw.customers
    """,
    "order_items": "SELECT * FROM r.raw.order_items",
    "order_payments": "SELECT * FROM r.raw.order_payments",
    "order_reviews": """
        SELECT review_id, order_id, review_score,
               review_comment_title IS NOT NULL   AS has_comment_title,
               review_comment_message IS NOT NULL AS has_comment_message,
               review_creation_date, review_answer_timestamp
        FROM r.raw.order_reviews
    """,
    "products": """
        SELECT p.*, t.product_category_name_english
        FROM r.raw.products p
        LEFT JOIN r.raw.product_category_name_translation t USING (product_category_name)
    """,
    "sellers": "SELECT seller_id, seller_zip_code_prefix, seller_city, seller_state FROM r.raw.sellers",
    "product_category_name_translation": "SELECT * FROM r.raw.product_category_name_translation",
    # mode() breaks ties arbitrarily (found by the repeatability test), so the most frequent city/state
    # is chosen with an explicit deterministic tie-break: highest count, then alphabetical.
    "geolocation_zip_prefix": """
        WITH base AS (
            SELECT geolocation_zip_code_prefix              AS zip_code_prefix,
                   count(*)                                  AS source_point_count,
                   count(DISTINCT geolocation_state)         AS distinct_state_count,
                   round(median(geolocation_lat), 3)         AS lat_median_3dp,
                   round(median(geolocation_lng), 3)         AS lng_median_3dp
            FROM r.raw.geolocation GROUP BY 1
        ),
        city AS (
            SELECT geolocation_zip_code_prefix AS zip_code_prefix, geolocation_city AS city_mode
            FROM r.raw.geolocation GROUP BY 1, 2
            QUALIFY row_number() OVER (PARTITION BY geolocation_zip_code_prefix
                                       ORDER BY count(*) DESC, geolocation_city ASC) = 1
        ),
        state AS (
            SELECT geolocation_zip_code_prefix AS zip_code_prefix, geolocation_state AS state_mode
            FROM r.raw.geolocation GROUP BY 1, 2
            QUALIFY row_number() OVER (PARTITION BY geolocation_zip_code_prefix
                                       ORDER BY count(*) DESC, geolocation_state ASC) = 1
        )
        SELECT b.zip_code_prefix, b.source_point_count, b.distinct_state_count, s.state_mode, c.city_mode,
               b.lat_median_3dp, b.lng_median_3dp
        FROM base b JOIN city c USING (zip_code_prefix) JOIN state s USING (zip_code_prefix)
        ORDER BY b.zip_code_prefix
    """,
    # Order-grain pre-aggregations (fanout-safe bridges).
    "order_item_totals": """
        SELECT order_id,
               count(*)                    AS item_count,
               count(DISTINCT seller_id)   AS seller_count,
               count(DISTINCT product_id)  AS product_count,
               sum(price)                  AS items_price_total,
               sum(freight_value)          AS items_freight_total,
               sum(price) + sum(freight_value) AS items_gross_total
        FROM r.raw.order_items GROUP BY order_id
    """,
    "order_payment_totals": """
        SELECT order_id,
               count(*)                       AS payment_row_count,
               count(DISTINCT payment_type)   AS payment_type_count,
               sum(payment_value)             AS payment_total,
               max(payment_installments)      AS max_installments,
               string_agg(DISTINCT payment_type, ',' ORDER BY payment_type) AS payment_types
        FROM r.raw.order_payments GROUP BY order_id
    """,
    "order_review_summary": """
        SELECT order_id,
               count(*)                 AS review_row_count,
               avg(review_score)        AS review_score_mean,
               -- deterministic tie-break on equal answer timestamps: highest review_id
               arg_max(review_score, strftime(review_answer_timestamp, '%Y-%m-%dT%H:%M:%S') || review_id)
                   AS latest_answered_review_score,
               min(review_score)        AS review_score_min,
               max(review_score)        AS review_score_max
        FROM r.raw.order_reviews GROUP BY order_id
    """,
}


def build(settings: Settings, *, skip_checksum_verification: bool = False) -> dict[str, Any]:
    source_manifest = load_manifest(settings)
    if source_manifest is None:
        raise BuildError("no source manifest; run `groundwork acquire` first")
    if not skip_checksum_verification:
        problems = verify_raw_files(settings)
        if problems:
            raise BuildError("raw files do not match manifest: " + "; ".join(problems))

    settings.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (settings.warehouse_path, settings.serving_path):
        for suffix in ("", ".wal"):
            Path(str(p) + suffix).unlink(missing_ok=True)

    con = duckdb.connect(str(settings.warehouse_path))
    try:
        raw_stats = _load_raw(con, settings)
    finally:
        con.close()

    key = _pseudonym_key(settings)
    rcon = duckdb.connect(str(settings.warehouse_path), read_only=True)
    try:
        rcat_row = rcon.execute("SELECT current_database()").fetchone()
        assert rcat_row is not None
        rc = f'"{rcat_row[0]}".raw'
        ids = [
            r[0]
            for r in rcon.execute(
                f"SELECT customer_id FROM {rc}.customers UNION SELECT customer_unique_id FROM {rc}.customers "
                f"UNION SELECT customer_id FROM {rc}.orders"
            ).fetchall()
            if r[0] is not None
        ]
    finally:
        rcon.close()
    map_file = settings.secrets_dir / "pseudonym_map.tmp.csv"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    with map_file.open("w", encoding="utf-8", newline="") as fh:
        fh.write("raw_value,pseudonym\n")
        for v in ids:
            fh.write(f"{v},{hmac.new(key, v.encode('utf-8'), sha256).hexdigest()[:32]}\n")

    scon = duckdb.connect(str(settings.serving_path))
    serving_stats: dict[str, int] = {}
    try:
        scon.execute(
            "CREATE TEMP TABLE pseudo_map AS SELECT * FROM read_csv(?, header=true, all_varchar=true)",
            [map_file.as_posix()],
        )
        map_file.unlink()
        scon.execute(
            "CREATE TEMP MACRO cf_pseudonym(x) AS (SELECT pseudonym FROM pseudo_map WHERE raw_value = x)"
        )
        scon.execute(f"ATTACH '{settings.warehouse_path.as_posix()}' AS r (READ_ONLY)")
        scat_row = scon.execute("SELECT current_database()").fetchone()
        assert scat_row is not None
        scat = scat_row[0]
        scon.execute(f'CREATE SCHEMA IF NOT EXISTS "{scat}".analytics')
        for name, sql in SERVING_SQL.items():
            scon.execute(f'CREATE OR REPLACE TABLE "{scat}".analytics.{name} AS {sql}')
            n = scon.execute(f'SELECT count(*) FROM "{scat}".analytics.{name}').fetchone()
            assert n is not None
            serving_stats[name] = n[0]
        scon.execute("DETACH r")
        scon.execute("DROP MACRO cf_pseudonym")
        scon.execute("DROP TABLE pseudo_map")
        scon.execute("CHECKPOINT")
    finally:
        scon.close()
        map_file.unlink(missing_ok=True)

    manifest = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duckdb_version": duckdb.__version__,
        "source_archive_sha256": source_manifest.get("archive_sha256"),
        "source_files_sha256": {f["file"]: f["sha256"] for f in source_manifest["files"]},
        "raw_row_counts": {t: s["rows"] for t, s in raw_stats.items()},
        "raw_schemas": {t: s["columns"] for t, s in raw_stats.items()},
        "serving_row_counts": serving_stats,
        "pseudonymization": "HMAC-SHA256 truncated to 32 hex chars; key from GROUNDWORK_PSEUDONYM_KEY "
        "or data/secrets/pseudonym.key (gitignored)",
    }
    build_manifest_path(settings).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info("warehouse build complete", extra={"serving_tables": len(serving_stats)})
    return manifest
