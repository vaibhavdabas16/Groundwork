"""Proposal staging, grounding, validation gating and context deployment against the REAL context
repository and REAL warehouse schema. The improvement engine's model is a SCRIPTED test double
(labelled); GitHub is the in-memory contract fake. Nothing here is evidence of a live PR or model."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from contextforge.agent.providers.scripted import ScriptedProvider, turn
from contextforge.config.settings import Settings
from contextforge.product.audit import AuditLog
from contextforge.product.config import ContextReviewer, ProductConfig
from contextforge.product.db import Database
from contextforge.product.deployment import ContextDeployer, DeploymentRejected
from contextforge.product.failures import FailureCategory, FailureRecord, FailureStore
from contextforge.product.proposals.engine import EngineBlocked, ImprovementEngine
from contextforge.product.proposals.models import (
    AddMetricCaveat,
    ChangeMetricSql,
    ContextProposal,
    ProposalDraft,
    SetColumnDescription,
    State,
)
from contextforge.product.proposals.patches import PatchError, stage
from contextforge.product.proposals.store import ProposalStore
from contextforge.product.proposals.validation import ProposalValidator, static_checks

pytestmark = pytest.mark.integration


def _stage(s: Settings, tmp: Path, ops: list[Any]) -> Any:
    return stage(
        repo_root=s.root,
        context_dir=s.context_dir,
        serving_path=s.serving_path,
        ops=ops,
        author="agent.contextforge_improver",
        evidence_refs=["trace:t1"],
        work_dir=tmp / "repo",
    )


def test_staging_checks_real_schema_and_keeps_objects_draft(real_warehouse: Settings, tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="does not exist"):
        _stage(
            real_warehouse,
            tmp_path,
            [
                SetColumnDescription(
                    asset_id="asset.analytics.order_payments",
                    column="refund_amount",
                    description="Refund amount per payment in BRL.",
                )
            ],
        )
    with pytest.raises(PatchError, match="not a governed serving asset"):
        _stage(
            real_warehouse,
            tmp_path,
            [
                SetColumnDescription(
                    asset_id="asset.raw.customers",
                    column="customer_id",
                    description="raw identifiers are not documentable here",
                )
            ],
        )
    res = _stage(
        real_warehouse,
        tmp_path,
        [
            SetColumnDescription(
                asset_id="asset.analytics.customers",
                column="customer_unique_key",
                description="Person-level pseudonym; use it to count people and repeat buyers.",
            ),
            ChangeMetricSql(
                metric_id="metric.order_count",
                sql="SELECT count(DISTINCT order_id) AS order_count FROM analytics.orders",
                double_counting_check="count distinct order_id",
            ),
        ],
    )
    assert res.validation_errors == [] and res.review_tier == "elevated"
    added = "".join(
        line for d in res.diffs for line in d.unified_diff.splitlines(keepends=True) if line.startswith("+")
    )
    assert "verified" not in added and "certified_by" not in added
    assert all(d.path.startswith("context/") for d in res.diffs)


def _failure(
    store: FailureStore, cat: FailureCategory, ids: list[str], q: str = "How many customers bought twice?"
) -> str:
    from contextforge.product.failures import REMEDIATION

    rem, kind = REMEDIATION[cat]
    rec = FailureRecord(
        failure_id=f"fail-{cat.value[:6].lower()}",
        trace_id=f"trace-{cat.value[:6].lower()}",
        category=cat,
        question=q,
        safe_explanation="x",
        context_ids=ids,
        evidence={"outcome": "INSUFFICIENT_CONTEXT", "queries": []},
        retryable=False,
        remediation=rem,
        context_fix_appropriate=kind is not None,
        fix_kind=kind,
        context_version="ctx",
    )
    return store.record(rec).failure_id


def _engine(
    s: Settings, db: Database, script: Any | None
) -> tuple[ImprovementEngine, ProposalStore, FailureStore]:
    audit = AuditLog(db)
    ps, fs = ProposalStore(db, audit), FailureStore(db, audit)
    eng = ImprovementEngine(
        s,
        config=ProductConfig(),
        proposals=ps,
        failures=fs,
        audit=audit,
        traces=None,
        context_dir=lambda: s.context_dir,
        provider_factory=(lambda: ScriptedProvider(script)) if script else None,
    )
    return eng, ps, fs


def test_infrastructure_failures_never_produce_context_changes(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    eng, ps, fs = _engine(real_warehouse, Database(tmp_path / "ops.sqlite"), script=None)
    for cat in (
        FailureCategory.DATABASE_UNAVAILABLE,
        FailureCategory.MODEL_UNAVAILABLE,
        FailureCategory.QUERY_RESOURCE_LIMIT,
        FailureCategory.PERMISSION_DENIED,
    ):
        assert eng.process(_failure(fs, cat, ["asset.analytics.orders"])) is None
    assert ps.list() == []


def test_missing_source_data_becomes_human_decision_not_invented_metric(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    eng, _ps, fs = _engine(real_warehouse, Database(tmp_path / "ops.sqlite"), script=None)
    row = eng.process(
        _failure(
            fs,
            FailureCategory.INSUFFICIENT_SOURCE_DATA,
            ["unsupported.net_revenue"],
            "What is true net revenue after refunds?",
        )
    )
    assert row is not None and row.proposal.draft.kind == "human_decision"
    assert row.proposal.draft.operations == [] and row.proposal.diffs == []
    dr = row.proposal.draft.decision_request
    assert dr is not None and dr.decision_owner == "team.data_governance" and "not available" in dr.question


def test_llm_draft_must_be_grounded_in_observed_evidence(real_warehouse: Settings, tmp_path: Path) -> None:
    op = {
        "op": "set_column_description",
        "asset_id": "asset.analytics.customers",
        "column": "customer_unique_key",
        "description": "Person-level pseudonym; one value per real customer. Use it for repeat-buyer counts.",
    }

    def draft(evidence: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "kind": "context_change",
            "summary": "Clarify person-level customer key",
            "deficiency": "Agent counted customer_key (per order) as people.",
            "operations": [op],
            "evidence": evidence,
            "assumptions": [],
            "risks": [],
        }

    calls = {"n": 0}

    def script(h: list[Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:  # fabricated evidence: a query id it never ran
            return turn(
                (
                    "submit_proposal",
                    draft([{"kind": "governed_query", "ref": "q-made-up", "detail": "2997 repeat people"}]),
                )
            )
        if calls["n"] == 2:
            return turn(("get_table_columns", {"table": "customers"}))
        if calls["n"] == 3:
            return turn(
                (
                    "run_governed_query",
                    {
                        "sql": "SELECT count(*) AS n FROM (SELECT customer_unique_key FROM analytics.customers GROUP BY 1 HAVING count(*) > 1) t"  # noqa: E501 - long literal
                    },
                )
            )
        out = json.loads(h[-1][0].content)["untrusted_tool_output"]
        return turn(
            (
                "submit_proposal",
                draft(
                    [
                        {
                            "kind": "schema",
                            "ref": "asset.analytics.customers:customer_unique_key",
                            "detail": "column exists",
                        },
                        {
                            "kind": "governed_query",
                            "ref": out["query_id"],
                            "detail": f"{out['rows'][0][0]} people have >1 customer_key",
                        },
                    ]
                ),
            )
        )

    db = Database(tmp_path / "ops.sqlite")
    eng, ps, fs = _engine(real_warehouse, db, script)
    row = eng.process(_failure(fs, FailureCategory.MISSING_COLUMN_DESCRIPTION, ["asset.analytics.customers"]))
    assert row is not None and row.state == State.PROPOSED and calls["n"] == 4
    assert [d.path for d in row.proposal.diffs] == ["context/assets/serving/dimensions.yaml"]
    assert row.proposal.author_type == "agent" and row.proposal.review_tier == "standard"
    # same failure again -> same proposal (no duplicate)
    calls["n"] = 1
    again = eng.process(
        _failure(fs, FailureCategory.MISSING_COLUMN_DESCRIPTION, ["asset.analytics.customers"])
    )
    assert again is not None and again.proposal_id == row.proposal_id and len(ps.list()) == 1


def test_engine_without_model_is_blocked(real_warehouse: Settings, tmp_path: Path) -> None:
    eng, _, fs = _engine(real_warehouse, Database(tmp_path / "ops.sqlite"), script=None)
    with pytest.raises(EngineBlocked):
        eng.process(_failure(fs, FailureCategory.MISSING_COLUMN_DESCRIPTION, ["asset.analytics.customers"]))


def _proposal_row(ps: ProposalStore, ops: list[Any]) -> Any:
    p = ContextProposal(
        proposal_id="cfp-val",
        revision=1,
        category="MISSING_BUSINESS_DEFINITION",
        title="t",
        draft=ProposalDraft(
            kind="context_change", summary="add caveat", deficiency="missing caveat", operations=ops
        ),
        affected_ids=["metric.order_count"],
        domains=["commerce.orders"],
        review_tier="standard",
        diffs=[],
        base_context_version="ctx",
        proposed_context_version=None,
        author="agent.contextforge_improver",
        author_type="agent",
        trace_ids=["t1"],
        failure_ids=["f1"],
        eval_case_ids=[],
        created_at="2026-11-20",
    )
    return ps.create(p, "sig-val")[0]


def test_validation_with_test_double_is_blocked_never_promoted(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    db = Database(tmp_path / "ops.sqlite")
    audit = AuditLog(db)
    ps = ProposalStore(db, audit)
    row = _proposal_row(
        ps,
        [AddMetricCaveat(metric_id="metric.order_count", text="Includes canceled and unavailable orders.")],
    )
    from contextforge.agent.schemas import AgentConfig

    v = ProposalValidator(
        real_warehouse,
        ps,
        audit,
        context_dir=lambda: real_warehouse.context_dir,
        agent_config=AgentConfig(),
        provider_factory=lambda: ScriptedProvider(
            lambda h: turn(("submit_response", {"outcome": "INSUFFICIENT_CONTEXT", "answer": "x"}))
        ),
        repetitions=1,
    )
    rep = v.validate(row.proposal_id)
    assert rep["verdict"] == "BLOCKED" and "plumbing_test" in rep["blocked"]
    assert ps.get(row.proposal_id).state == State.BLOCKED  # type: ignore[union-attr]


def test_static_validation_fails_when_ops_no_longer_apply(real_warehouse: Settings, tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.sqlite")
    ps = ProposalStore(db, AuditLog(db))
    row = _proposal_row(
        ps, [AddMetricCaveat(metric_id="metric.does_not_exist", text="Some caveat that cannot apply.")]
    )
    problems, staged = static_checks(row, real_warehouse, real_warehouse.context_dir, tmp_path / "w")
    assert problems and staged is None


def test_deploy_validates_switches_atomically_rejects_invalid_and_rolls_back(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    db = Database(tmp_path / "ops.sqlite")
    audit = AuditLog(db)
    alerts: list[str] = []
    s = real_warehouse.model_copy(update={"data_dir": tmp_path / "data"})
    dep = ContextDeployer(s, db, audit, releases_root=tmp_path / "releases", alert=alerts.append)
    v1 = dep.deploy(real_warehouse.context_dir, actor="tester")
    assert dep.verify().version == v1.version
    # candidate 2: valid change
    c2 = tmp_path / "c2" / "context"
    shutil.copytree(real_warehouse.context_dir, c2)
    p = c2 / "unsupported" / "concepts.yaml"
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "guidance: Say it is not available.", "guidance: Say it is not available in this dataset.", 1
        ),
        encoding="utf-8",
    )
    v2 = dep.deploy(c2, actor="tester")
    assert v2.version != v1.version and dep.active().version == v2.version  # type: ignore[union-attr]
    # candidate 3: invalid (AI tries to self-certify) -> rejected, v2 stays active, alert fired
    c3 = tmp_path / "c3" / "context"
    shutil.copytree(real_warehouse.context_dir, c3)
    t = c3 / "glossary" / "terms.yaml"
    t.write_text(
        t.read_text(encoding="utf-8").replace(
            "certification: {status: draft}",
            "certification: {status: verified, certified_by: agent.claude_code}",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeploymentRejected):
        dep.deploy(c3, actor="tester")
    assert dep.active().version == v2.version and alerts  # type: ignore[union-attr]
    assert any(e["action"] == "context.deploy_rejected" for e in audit.events())
    # rollback to v1
    assert dep.rollback(actor="tester").version == v1.version
    # tampering with an active release is detected
    rel = dep.active()
    assert rel is not None
    f = next(rel.context_dir.rglob("*.yaml"))
    f.write_text(f.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(DeploymentRejected, match="hash"):
        dep.verify()
    assert audit.verify().ok


def test_reviewer_config_requires_real_humans(real_warehouse: Settings, tmp_path: Path) -> None:
    from contextforge.context.loader import load_context
    from contextforge.product.config import check_against_context

    cfg = ProductConfig(
        context_reviewers=[ContextReviewer(slack_user_id="UAAA111", owner_id="team.data_governance")]
    )
    assert check_against_context(cfg, load_context(real_warehouse.context_dir))
