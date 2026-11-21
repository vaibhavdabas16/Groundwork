"""Context-layer validation: the real repository is valid; controlled mutations are rejected."""

from __future__ import annotations

from pathlib import Path

import yaml

from groundwork.context.loader import load_context
from groundwork.context.models import Status
from groundwork.context.validation import errors, validate_directory

ROOT = Path(__file__).resolve().parents[2]


def codes(ctx: Path) -> set[str]:
    _, issues = validate_directory(ctx)
    return {i.code for i in errors(issues)}


def edit(path: Path, fn) -> None:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_real_context_is_valid() -> None:
    store, issues = validate_directory(ROOT / "context")
    assert errors(issues) == [], [str(i) for i in errors(issues)]
    assert len(store.assets) >= 12 and len(store.metrics) >= 10 and len(store.terms) >= 10
    assert len(store.unsupported) >= 5 and len(store.joins) >= 5
    assert store.version.startswith("ctx-")


def test_all_ai_authored_content_is_not_verified() -> None:
    """Phase 1 content was authored by an AI agent: nothing may be verified."""
    store = load_context(ROOT / "context")
    for d in (store.assets, store.terms, store.metrics, store.joins):
        for obj in d.values():
            assert obj.certification.status != Status.verified, obj.id


def test_context_version_is_deterministic_and_content_sensitive(context_copy: Path) -> None:
    v1 = load_context(context_copy).content_hash
    assert load_context(context_copy).content_hash == v1
    p = context_copy / "glossary" / "terms.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# change\n", encoding="utf-8")
    assert load_context(context_copy).content_hash != v1


def test_invalid_asset_reference_rejected(context_copy: Path) -> None:
    edit(
        context_copy / "metrics" / "orders.yaml",
        lambda d: d["items"][0].update(assets=["asset.analytics.nope"]),
    )
    assert "broken_reference" in codes(context_copy)


def test_broken_join_rejected(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        d["items"][0]["join_columns"][0]["left"] = "no_such_column"
        d["items"][1]["right"] = "asset.analytics.missing"

    edit(context_copy / "joins" / "joins.yaml", f)
    assert "broken_join" in codes(context_copy)


def test_duplicate_identifier_rejected(context_copy: Path) -> None:
    edit(context_copy / "glossary" / "terms.yaml", lambda d: d["items"].append(dict(d["items"][0])))
    assert "duplicate_id" in codes(context_copy)


def test_invalid_status_rejected(context_copy: Path) -> None:
    edit(
        context_copy / "glossary" / "terms.yaml",
        lambda d: d["items"][0].update(certification={"status": "approved-ish"}),
    )
    assert "schema_error" in codes(context_copy)


def test_unknown_field_rejected(context_copy: Path) -> None:
    edit(context_copy / "glossary" / "terms.yaml", lambda d: d["items"][0].update(definiton="typo field"))
    assert "schema_error" in codes(context_copy)


def test_circular_metric_dependency_rejected(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        by = {m["id"]: m for m in d["items"]}
        by["metric.order_count"]["depends_on"] = ["metric.cancellation_rate"]

    edit(context_copy / "metrics" / "orders.yaml", f)
    assert "circular_dependency" in codes(context_copy)


def test_verified_with_placeholder_owner_and_no_certifier_rejected(context_copy: Path) -> None:
    edit(
        context_copy / "glossary" / "terms.yaml",
        lambda d: d["items"][0].update(certification={"status": "verified"}),
    )
    c = codes(context_copy)
    assert {"verified_placeholder_owner", "verified_missing_certification"} <= c


def test_ai_agent_cannot_certify(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        d["items"][0]["certification"] = {
            "status": "verified",
            "certified_by": "agent.claude_code",
            "certified_at": "2026-11-19T00:00:00Z",
            "decision_ref": "self",
        }

    edit(context_copy / "glossary" / "terms.yaml", f)
    c = codes(context_copy)
    assert "invalid_certifier" in c and "self_certification" in c


def test_human_certification_accepted_when_complete(context_copy: Path) -> None:
    """Positive control: a real human certifier + non-placeholder owner makes 'verified' valid."""

    def owners(d):  # type: ignore[no-untyped-def]
        d["items"].append(
            {"id": "person.test_reviewer", "kind": "human", "name": "Test Reviewer", "can_certify": True}
        )
        d["items"].append({"id": "team.test_owner", "kind": "team", "name": "Test Owner"})

    edit(context_copy / "ownership" / "owners.yaml", owners)

    def term(d):  # type: ignore[no-untyped-def]
        t = next(x for x in d["items"] if x["id"] == "term.boleto")
        t["owner"] = "team.test_owner"
        t["certification"] = {
            "status": "verified",
            "certified_by": "person.test_reviewer",
            "certified_at": "2026-11-19T00:00:00Z",
            "decision_ref": "PR#test",
        }

    edit(context_copy / "glossary" / "terms.yaml", term)
    assert codes(context_copy) == set()


def test_placeholder_or_agent_cannot_have_can_certify(context_copy: Path) -> None:
    edit(context_copy / "ownership" / "owners.yaml", lambda d: d["items"][0].update(can_certify=True))
    assert "invalid_certifier" in codes(context_copy)


def test_draft_cannot_carry_certification_fields(context_copy: Path) -> None:
    edit(
        context_copy / "glossary" / "terms.yaml",
        lambda d: d["items"][0].update(
            certification={"status": "draft", "certified_by": "team.data_platform"}
        ),
    )
    assert "draft_with_certification" in codes(context_copy)


def test_deprecated_requires_reason(context_copy: Path) -> None:
    edit(
        context_copy / "glossary" / "terms.yaml",
        lambda d: d["items"][0].update(certification={"status": "deprecated"}),
    )
    assert "deprecated_missing_reason" in codes(context_copy)


def test_unsafe_metric_sql_rejected(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        d["items"][0]["sql"] = "SELECT * FROM read_csv('data/raw/olist/olist_customers_dataset.csv')"
        d["items"][1]["sql"] = "DELETE FROM analytics.orders"
        d["items"][2]["sql"] = "SELECT customer_unique_id FROM analytics.orders"

    edit(context_copy / "metrics" / "orders.yaml", f)
    _, issues = validate_directory(context_copy)
    unsafe = [i for i in errors(issues) if i.code == "unsafe_metric_sql"]
    assert len(unsafe) == 3


def test_metric_reading_undeclared_asset_rejected(context_copy: Path) -> None:
    edit(
        context_copy / "metrics" / "orders.yaml",
        lambda d: d["items"][0].update(
            sql="SELECT count(*) FROM analytics.orders o JOIN analytics.order_items i ON i.order_id = o.order_id"
        ),
    )
    assert "metric_undeclared_asset" in codes(context_copy)


def test_serving_asset_with_pii_column_rejected(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        d["sensitivity"] = "pii"
        d["columns"].append(
            {
                "name": "review_comment_message",
                "data_type": "VARCHAR",
                "description": "x",
                "sensitivity": "pii",
            }
        )

    edit(context_copy / "assets" / "serving" / "order_reviews.yaml", f)
    assert "serving_exposes_sensitive" in codes(context_copy)


def test_asset_less_sensitive_than_column_rejected(context_copy: Path) -> None:
    edit(context_copy / "assets" / "serving" / "orders.yaml", lambda d: d.update(sensitivity="public"))
    assert "contradictory_sensitivity" in codes(context_copy)


def test_contradictory_verified_definitions_rejected(context_copy: Path) -> None:
    def owners(d):  # type: ignore[no-untyped-def]
        d["items"].append({"id": "person.reviewer", "kind": "human", "name": "R", "can_certify": True})
        d["items"].append({"id": "team.real", "kind": "team", "name": "Real"})

    edit(context_copy / "ownership" / "owners.yaml", owners)
    cert = {
        "status": "verified",
        "certified_by": "person.reviewer",
        "certified_at": "2026-11-19T00:00:00Z",
        "decision_ref": "x",
    }

    def terms(d):  # type: ignore[no-untyped-def]
        for t in d["items"][:2]:
            t.update(owner="team.real", certification=cert)
            t["synonyms"] = [*t.get("synonyms", []), "the same word"]

    edit(context_copy / "glossary" / "terms.yaml", terms)
    assert "contradictory_definitions" in codes(context_copy)


def test_policy_cannot_grant_raw_assets_or_dangerous_functions(context_copy: Path) -> None:
    def f(d):  # type: ignore[no-untyped-def]
        d["roles"][0]["queryable_assets"].append("asset.raw.customers")
        d["query"]["allowed_functions"].append("READ_CSV")

    edit(context_copy / "policies" / "policy.yaml", f)
    _, issues = validate_directory(context_copy)
    msgs = " ".join(i.message for i in errors(issues) if i.code == "unsafe_policy")
    assert "raw asset" in msgs and "READ_CSV" in msgs


def test_missing_policy_rejected(context_copy: Path) -> None:
    (context_copy / "policies" / "policy.yaml").unlink()
    assert "policy_missing" in codes(context_copy)


def test_missing_required_policy_field_rejected(context_copy: Path) -> None:
    edit(context_copy / "policies" / "policy.yaml", lambda d: d.pop("retention"))
    assert "schema_error" in codes(context_copy)


def test_missing_sensitivity_class_rejected(context_copy: Path) -> None:
    edit(
        context_copy / "policies" / "policy.yaml",
        lambda d: d.update(sensitivity_classes=d["sensitivity_classes"][:2]),
    )
    assert "policy_missing" in codes(context_copy)


def test_yaml_error_reported_not_raised(context_copy: Path) -> None:
    (context_copy / "glossary" / "broken.yaml").write_text("items: [unclosed", encoding="utf-8")
    assert "yaml_error" in codes(context_copy)
