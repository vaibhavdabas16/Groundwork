"""Experimental-condition isolation: RAW_SCHEMA and DESCRIPTIVE_METADATA must not leak curated
semantics through any tool result, error, warning, search score/summary or server instruction.

The probe exercises every agent tool with many inputs (all glossary/metric/unsupported names and
synonyms, every asset, every join pair, policy lookups, valid/invalid/CURRENT_DATE SQL) and checks
the serialized responses for every forbidden string derived from the governed context.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from groundwork.config.settings import ContextMode, Settings
from groundwork.context.loader import ContextStore
from groundwork.service.core import GroundworkService

pytestmark = pytest.mark.integration


def _long(s: str | None) -> list[str]:
    s = (s or "").strip()
    return [s[:60]] if len(s) >= 25 else []


def forbidden_strings(store: ContextStore, *, allow_descriptions: bool) -> set[str]:
    out: set[str] = set()
    for a in store.assets.values():
        out.update(_long(a.grain))
        for q in a.quality_warnings:
            out.update(_long(q.description))
        if not allow_descriptions:
            out.update(_long(a.description))
            for c in a.columns:
                out.update(_long(c.description))
        if a.layer == "raw" and not allow_descriptions:
            out.add(a.id)
    for m in store.metrics.values():
        out.update(_long(m.definition) + _long(m.sql) + _long(m.business_purpose))
        out.add(m.id)
    for t in store.terms.values():
        out.update(_long(t.definition) + _long(t.business_interpretation))
        out.add(t.id)
    for j in store.joins.values():
        out.update(_long(j.grain_note) + _long(j.fanout_warning))
        out.add(j.id)
    for u in store.unsupported.values():
        out.update(_long(u.reason) + _long(u.guidance))
        out.add(u.id)
    for sc in store.policy.sensitivity_classes:
        out.update(_long(sc.handling))
    out.update(["DRAFT", "draft", "verified", "deprecated", "fanout"])
    if not allow_descriptions:
        # dataset coverage facts appear in table descriptions (allowed only in DESCRIPTIVE_METADATA)
        out.update(["Olist", "2016-09", "2018-10"])
    return out


def probe(svc: GroundworkService) -> list[tuple[str, dict[str, Any]]]:
    st = svc.store
    out: list[tuple[str, dict[str, Any]]] = []
    names = set()
    for obj in [*st.metrics.values(), *st.terms.values(), *st.unsupported.values()]:
        names.add(obj.name)
        names.update(getattr(obj, "synonyms", []))
        names.add(obj.id)
    names.update(
        ["revenue", "orders", "customers", "payments", "delivery", "review score", "churn", "freight"]
    )
    rc = {"request_id": "leak-probe"}
    for n in sorted(names):
        out.append((f"search:{n}", svc.search_assets(n, None, 50).model_dump(mode="json")))
        out.append(
            (
                f"search_typed:{n}",
                svc.search_assets(
                    n, {"types": ["metric", "glossary_term", "join", "unsupported_concept"]}, 50
                ).model_dump(mode="json"),
            )
        )
        out.append((f"define:{n}", svc.get_definition(n).model_dump(mode="json")))
    for a in st.assets.values():
        out.append((f"asset:{a.id}", svc.get_asset(a.id).model_dump(mode="json")))
        out.append((f"policy:{a.id}", svc.get_policy(a.id).model_dump(mode="json")))
        for c in a.columns[:3]:
            out.append(
                (f"policy:{a.id}:{c.name}", svc.get_policy(f"{a.id}:{c.name}").model_dump(mode="json"))
            )
    serving = [a.id for a in st.assets.values() if a.layer == "serving"]
    for x, y in itertools.islice(itertools.permutations(serving, 2), 40):
        out.append((f"join:{x}->{y}", svc.get_join_path(x, y).model_dump(mode="json")))
    for sql in (
        "SELECT count(*) FROM analytics.orders WHERE order_purchase_timestamp < current_date",
        "SELECT sum(p.payment_value) FROM analytics.order_payments p JOIN analytics.order_items i ON i.order_id = p.order_id",
        "SELECT customer_zip_code_prefix FROM analytics.customers",
        "SELECT * FROM raw.customers",
        "SELECT nope FROM analytics.orders",
        "SELECT order_status, count(*) FROM analytics.orders GROUP BY 1",
    ):
        out.append((f"explain:{sql}", svc.explain_query(sql, rc).model_dump(mode="json")))
        out.append((f"query:{sql}", svc.run_query(sql, rc).model_dump(mode="json")))
    return out


def _values(obj: Any) -> str:
    """All string VALUES (not field names) of a response, joined."""
    if isinstance(obj, dict):
        return " | ".join(_values(v) for v in obj.values())
    if isinstance(obj, list):
        return " | ".join(_values(v) for v in obj)
    return obj if isinstance(obj, str) else ""


def _svc(settings: Settings, tmp: Path, mode: ContextMode) -> GroundworkService:
    return GroundworkService(settings.model_copy(update={"data_dir": tmp}), context_mode=mode)


@pytest.mark.parametrize("mode", [ContextMode.raw_schema, ContextMode.descriptions])
def test_restricted_modes_do_not_leak_governed_context(
    real_warehouse: Settings, tmp_path: Path, mode: ContextMode
) -> None:
    svc = _svc(real_warehouse, tmp_path, mode)
    try:
        forbidden = forbidden_strings(svc.store, allow_descriptions=mode == ContextMode.descriptions)
        leaks = []
        for label, resp in probe(svc):
            text = _values(resp)
            probe_input = label.split(":", 1)[1]
            for f in forbidden:
                if f in text and f not in probe_input:  # echoing the caller's own input is not a leak
                    leaks.append((label, f))
        assert not leaks, f"{len(leaks)} leaks, e.g. {leaks[:8]}"
    finally:
        svc.close()


def test_restricted_modes_expose_schema_and_identical_safety(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    raw = _svc(real_warehouse, tmp_path / "a", ContextMode.raw_schema)
    desc = _svc(real_warehouse, tmp_path / "b", ContextMode.descriptions)
    full = _svc(real_warehouse, tmp_path / "c", ContextMode.full_context)
    try:
        a = raw.get_asset("analytics.orders")
        assert a.ok and {c["name"] for c in a.asset["columns"]} >= {"order_id", "order_status"}
        assert all(set(c) == {"name", "data_type", "nullable"} for c in a.asset["columns"])
        assert "description" not in a.asset and a.certification_status is None and not a.warnings
        d = desc.get_asset("analytics.orders")
        assert d.asset["description"] and all("description" in c for c in d.asset["columns"])
        assert "grain" not in d.asset and "quality_warnings" not in d.asset
        assert raw.get_definition("revenue").error.code == "not_available_in_context_mode"
        assert (
            desc.get_join_path("analytics.orders", "analytics.order_items").error.code
            == "not_available_in_context_mode"
        )
        assert raw.get_asset("asset.raw.customers").error.code == "not_found"
        hits = raw.search_assets("payment value", None, 10)
        assert hits.results and all(
            r.type in ("asset", "column") and r.certification_status is None for r in hits.results
        )
        # identical permissions/safety in every mode
        for svc in (raw, desc, full):
            assert svc.run_query("SELECT customer_unique_key FROM analytics.customers LIMIT 1").ok
            assert svc.run_query("SELECT * FROM raw.customers").error.code.startswith("sql_rejected")
            assert svc.policy.catalog().relations == full.policy.catalog().relations
        # dataset-specific CURRENT_DATE note only in full_context
        sql = "SELECT count(*) FROM analytics.orders WHERE order_purchase_timestamp < current_date"
        assert any("2018-10" in w for w in full.run_query(sql).warnings)
        assert not any("2018" in w for w in raw.run_query(sql).warnings)
        assert full.get_definition("revenue").resolution == "unsupported_concept"
    finally:
        for s in (raw, desc, full):
            s.close()
