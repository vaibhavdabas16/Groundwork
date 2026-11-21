"""Retrieval and policy behaviour against the real context repository (no database needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from groundwork.context.loader import load_context
from groundwork.context.models import Capability, Status
from groundwork.query.executor import redact_value
from groundwork.search.index import SearchIndex, tokenize
from groundwork.security.policy import AccessDenied, PolicyEngine

ROOT = Path(__file__).resolve().parents[2]
STORE = load_context(ROOT / "context")
INDEX = SearchIndex(STORE)


def ids(query: str, **kw) -> list[str]:  # type: ignore[no-untyped-def]
    return [h.doc.id for h in INDEX.search(query, **kw)]


def test_tokenizer_is_deterministic() -> None:
    assert (
        tokenize("Order_Items deliveries")
        == tokenize("order items deliveries")
        == ["order", "item", "delivery"]
    )


def test_search_tables_by_name_and_description() -> None:
    assert ids("order items", types={"asset"})[0] == "asset.analytics.order_items"
    assert "asset.analytics.sellers" in ids("merchant", types={"asset"})


def test_search_columns() -> None:
    top = ids("freight value", types={"column"})
    assert "asset.analytics.order_items:freight_value" in top[:3]


def test_search_synonyms_find_metrics_and_terms() -> None:
    assert ids("cancelled orders")[0] in {"metric.canceled_order_count", "term.canceled_order"}
    assert "metric.on_time_delivery_rate" in ids("otd")
    assert "term.customer" in ids("buyer")


def test_search_surfaces_unsupported_concepts() -> None:
    assert ids("churn")[0] == "unsupported.churn"
    assert ids("gmv")[0] == "unsupported.gmv"


def test_deprecated_surfaced_and_filterable() -> None:
    hits = INDEX.search("payment value item join")
    dep = [h for h in hits if h.doc.status == "deprecated"]
    assert dep and dep[0].doc.id == "metric.payment_value_naive_item_join"
    assert "metric.payment_value_naive_item_join" not in ids(
        "payment value item join", include_deprecated=False
    )


def test_visibility_filter_hides_drafts() -> None:
    hits = INDEX.search("order count", visible_statuses={Status.verified}, include_deprecated=False)
    # No draft (or deprecated) definitions leak into a verified-only view...
    assert all(h.doc.status not in ("draft", "deprecated") for h in hits)
    assert "metric.order_count" not in [h.doc.id for h in hits]
    # ...but the unsupported/ambiguous register (guidance, not a definition) stays visible by design.
    assert hits and all(h.doc.type == "unsupported_concept" for h in hits)


def test_results_carry_source_and_status() -> None:
    h = INDEX.search("review score")[0]
    assert h.doc.source_file and h.doc.source_file.startswith("context/")
    assert h.doc.status in {"draft", "verified", "deprecated", None}
    assert h.score > 0 and h.matched_terms


def test_missing_context_returns_nothing() -> None:
    assert ids("xylophone quaternion") == []


def test_search_is_reproducible() -> None:
    assert (
        ids("delivery delay")
        == ids("delivery delay")
        == [h.doc.id for h in SearchIndex(STORE).search("delivery delay")]
    )


# ---------------- policy ----------------
def test_analyst_principal_capabilities_and_catalog() -> None:
    p = PolicyEngine(STORE, "principal.local_analyst", 1000)
    assert p.has(Capability.query_execute) and not p.has(Capability.context_admin)
    cat = p.catalog()
    assert "analytics.orders" in cat.relations
    assert not any(q.startswith("raw.") for q in cat.relations)
    assert "customer_unique_key" in cat.relations["analytics.orders"]


def test_strict_principal_sees_no_draft_assets() -> None:
    p = PolicyEngine(STORE, "principal.local_strict", 1000)
    assert p.queryable_assets() == []  # nothing is verified yet
    assert p.catalog().relations == {}
    assert not p.can_see_status(Status.draft)


def test_metadata_principal_cannot_query() -> None:
    p = PolicyEngine(STORE, "principal.local_metadata", 1000)
    with pytest.raises(AccessDenied):
        p.require_tool("run_query")
    p.require_tool("search_assets")


def test_unknown_tool_and_unknown_principal_denied() -> None:
    p = PolicyEngine(STORE, "principal.local_analyst", 1000)
    with pytest.raises(AccessDenied):
        p.require_tool("drop_everything")
    with pytest.raises(AccessDenied):
        PolicyEngine(STORE, "principal.admin_by_wishful_thinking", 1000)


def test_raw_sensitive_columns_never_queryable() -> None:
    p = PolicyEngine(STORE, "principal.local_analyst", 1000)
    acc = p.column_access(STORE.assets["asset.raw.customers"], "customer_unique_id")
    assert not acc.queryable and acc.sensitivity == "restricted"


def test_result_boundary_redaction() -> None:
    assert redact_value("contact joao@example.com now")[0] == "contact [REDACTED:email] now"
    assert redact_value("cpf 123.456.789-09")[1] == 1
    assert redact_value(42) == (42, 0)
    assert redact_value("2018-10-17T17:30:18") == ("2018-10-17T17:30:18", 0)
