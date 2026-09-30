"""Render the data-quality report (docs/data_quality_report.md) from executed check results."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GRAIN = {
    "orders": "one row per order_id",
    "customers": "one row per customer_id - which is issued PER ORDER (not per person)",
    "order_items": "one row per (order_id, order_item_id)",
    "order_payments": "one row per (order_id, payment_sequential)",
    "order_reviews": "one row per (review_id, order_id); neither column is unique alone",
    "products": "one row per product_id",
    "sellers": "one row per seller_id",
    "geolocation": "one row per source point; NOT unique by ZIP prefix",
    "product_category_name_translation": "one row per product_category_name",
}


def render(
    report: dict[str, Any], source_manifest: dict[str, Any] | None, build_manifest: dict[str, Any] | None
) -> str:
    lines = [
        "# Data quality report - Olist Brazilian E-Commerce",
        "",
        f"_Generated {datetime.now(UTC).isoformat(timespec='seconds')} by `contextforge quality`; every number below "
        "comes from SQL executed against the real warehouse (`data/manifests/dq_report.json`)._",
        "",
    ]
    if source_manifest:
        lines += [
            "## Source",
            "",
            f"- Dataset: {source_manifest['dataset']} ({source_manifest['source_page']})",
            f"- License: {source_manifest.get('license')} (verified from {source_manifest.get('license_verified_from')})",
            f"- Retrieved: {source_manifest['retrieved_at']} via `{source_manifest['acquisition_mode']}` "
            f"from {source_manifest['retrieved_from']}",
            f"- Archive SHA-256: `{source_manifest.get('archive_sha256')}`",
            "",
            "| file | SHA-256 | bytes |",
            "|---|---|---|",
        ]
        lines += [f"| {f['file']} | `{f['sha256']}` | {f['bytes']:,} |" for f in source_manifest["files"]]
        lines.append("")
    lines += ["## Tables, row counts and grain", "", "| table | rows | grain |", "|---|---|---|"]
    for t, p in report["profile"].items():
        lines.append(f"| raw.{t} | {p['rows']:,} | {GRAIN.get(t, '')} |")
    lines.append("")
    if build_manifest:
        lines += ["Serving (agent-facing) relations:", "", "| relation | rows |", "|---|---|"]
        lines += [f"| analytics.{t} | {n:,} |" for t, n in build_manifest["serving_row_counts"].items()]
        lines.append("")
    inv = [c for c in report["checks"] if c["kind"] == "invariant"]
    obs = [c for c in report["checks"] if c["kind"] == "observation"]
    lines += [
        "## Invariants (must hold)",
        "",
        f"{sum(1 for c in inv if c['passed'])}/{len(inv)} passed.",
        "",
        "| check | description | violations | result |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{c['id']}` | {c['description']} | {c['value']} | {'PASS' if c['passed'] else 'FAIL'} |"
        for c in inv
    ]
    lines += [
        "",
        "## Observed properties and violations of the real data",
        "",
        "These are documented, not 'fixed'. The baseline in `data/manifests/dq_baseline.json` detects upstream changes.",
        "",
        "| check | category | description | observed |",
        "|---|---|---|---|",
    ]
    for c in obs:
        v = c["value"]
        if c["id"] == "time.max_purchase_date_epoch":
            v = f"{v} ({datetime.fromtimestamp(v, UTC).strftime('%Y-%m-%d %H:%M:%S')})"
        elif c["id"].startswith("fanout.") and isinstance(v, int):
            v = f"{v} cents = BRL {v / 100:,.2f}"
        lines.append(f"| `{c['id']}` | {c['category']} | {c['description']} | {v} |")
    if report.get("observation_baseline_drift"):
        lines += ["", "**Baseline drift detected:**", ""]
        lines += [f"- `{k}`: {d}" for k, d in report["observation_baseline_drift"].items()]
    lines += ["", "## Null percentages and ranges", ""]
    for t, p in report["profile"].items():
        lines += [
            f"### raw.{t}",
            "",
            "| column | type | null % | ~distinct | min | max |",
            "|---|---|---|---|---|---|",
        ]
        for c, i in p["columns"].items():
            lines.append(
                f"| {c} | {i['type']} | {i['null_pct']} | {i['approx_distinct']:,} | {i.get('min', '')} | {i.get('max', '')} |"
            )
        lines.append("")
    lines += [
        "## Join and metric pitfalls (derived from the checks above)",
        "",
        "- **Order vs item grain**: monetary values live on `order_items` (price, freight) and `order_payments`; "
        "orders has none. Joining orders to items yields item grain.",
        "- **Payments x items x reviews**: joining all three on order_id inflates sum(payment_value) - see "
        "`fanout.naive_payment_item_review_join_inflation_cents`. Use `analytics.order_*_totals` / `order_review_summary`.",
        "- **customer_id is per order**: count people with customer_unique_id (`customer_unique_key` in serving).",
        "- **review_id is not unique** and some orders have multiple reviews.",
        "- **Geolocation is not keyed by ZIP prefix**: joining customers/sellers to raw geolocation fans out; use "
        "`analytics.geolocation_zip_prefix`.",
        "- **Status vs timestamps disagree** for a handful of orders; carrier timestamps sometimes precede purchase/approval.",
        "- **Historical data**: last purchase 2018-10-17. Relative windows must be anchored to the data, not today.",
        "- **Payments != items + freight** for some orders; payments also exist for orders without items.",
        "",
    ]
    return "\n".join(lines)


def write_report(
    path: Path,
    report: dict[str, Any],
    source_manifest: dict[str, Any] | None,
    build_manifest: dict[str, Any] | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report, source_manifest, build_manifest), encoding="utf-8")
