"""Proposal workflow against the REAL context repo + warehouse schema and the in-memory GitHub
contract fake. Exercises: PR body/scope, Slack review request, authorized approval, merge
eligibility gates (Slack approval, head SHA, checks, human GitHub review), merged-outside-workflow
refusal. NOT evidence of a real GitHub PR (see docs/PHASE_3_HANDOFF.md)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from groundwork.config.settings import Settings
from groundwork.product.audit import AuditLog
from groundwork.product.config import ContextReviewer, GitHubSettings, ProductConfig
from groundwork.product.db import Database
from groundwork.product.deployment import ContextDeployer
from groundwork.product.github import GitHubClient, StaticToken
from groundwork.product.proposals.models import ContextProposal, ProposalDraft, SetColumnDescription, State
from groundwork.product.proposals.store import ProposalStore
from groundwork.product.proposals.workflow import ProposalWorkflow

pytestmark = pytest.mark.integration

_spec = importlib.util.spec_from_file_location(
    "gh_fake", Path(__file__).resolve().parents[1] / "unit" / "test_github_contract.py"
)
assert _spec and _spec.loader
gh_fake = importlib.util.module_from_spec(_spec)
sys.modules["gh_fake"] = gh_fake
_spec.loader.exec_module(gh_fake)


def setup(s: Settings, tmp: Path) -> tuple[Any, ProposalStore, ProposalWorkflow, ProductConfig, list[Any]]:
    fake = gh_fake.FakeGitHub()
    fake.commits["c0"]["files"]["context/assets/serving/dimensions.yaml"] = "x"
    fake.trees["t0"]["context/assets/serving/dimensions.yaml"] = "x"
    db = Database(tmp / "ops.sqlite")
    audit = AuditLog(db)
    ps = ProposalStore(db, audit)
    cfg = ProductConfig(
        github=GitHubSettings(repository=gh_fake.REPO),
        context_reviewers=[
            ContextReviewer(
                slack_user_id="UREVIEW1",
                owner_id="person.reviewer",
                domains=["commerce.*"],
                github_login="rev-gh",
            )
        ],
    )
    notified: list[Any] = []
    wf = ProposalWorkflow(
        s,
        cfg,
        ps,
        audit,
        ContextDeployer(s, db, audit, releases_root=tmp / "rel"),
        github_factory=lambda: GitHubClient(
            gh_fake.REPO, StaticToken("t"), transport=httpx.MockTransport(fake), max_retries=0
        ),
        context_dir=lambda: s.context_dir,
        notify_review=lambda row: notified.append(row) or "123.456",
    )
    op = SetColumnDescription(
        asset_id="asset.analytics.customers",
        column="customer_unique_key",
        description="Person-level pseudonym; use it to count people and repeat buyers.",
    )
    p = ContextProposal(
        proposal_id="cfp-wf",
        revision=1,
        category="MISSING_COLUMN_DESCRIPTION",
        title="Clarify customer_unique_key",
        draft=ProposalDraft(
            kind="context_change",
            summary="Clarify person-level key",
            deficiency="customer_key counted as people",
            operations=[op],
            evidence=[
                {"kind": "schema", "ref": "asset.analytics.customers:customer_unique_key", "detail": "exists"}
            ],
        ),  # type: ignore[list-item]
        affected_ids=["asset.analytics.customers"],
        domains=["commerce.customers"],
        review_tier="standard",
        diffs=[],
        base_context_version="ctx",
        proposed_context_version=None,
        author="agent.groundwork_improver",
        author_type="agent",
        trace_ids=["t1"],
        failure_ids=["f1"],
        eval_case_ids=[],
        created_at="2026-11-20",
    )
    ps.create(p, "sig-wf")
    return fake, ps, wf, cfg, notified


def test_pr_is_opened_only_after_validation_and_is_idempotent(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    fake, ps, wf, _cfg, notified = setup(real_warehouse, tmp_path)
    with pytest.raises(ValueError, match="validated"):
        wf.open_pull_request("cfp-wf")
    ps.transition("cfp-wf", State.VALIDATING, actor="system.validator")
    ps.transition(
        "cfp-wf",
        State.AWAITING_REVIEW,
        actor="system.validator",
        updates={"validation_json": {"verdict": "AWAITING_REVIEW"}},
    )
    row = wf.open_pull_request("cfp-wf")
    row2 = wf.open_pull_request("cfp-wf")  # retry after a crash / duplicate trigger
    assert len(fake.pulls) == 1 and row.raw["pr_number"] == row2.raw["pr_number"] == 1
    assert len(notified) == 1  # review requested once
    body = fake.pulls[0]["body"]
    for section in (
        "Context problem",
        "Evidence",
        "Proposed fix",
        "Before / after context diff",
        "Evaluation",
        "Targeted regression tests",
        "Security impact",
        "Approval requirements",
        "Traces",
    ):
        assert section in body
    head_files = fake.trees[fake.commits[fake.refs["groundwork/proposal-cfp-wf"]]["tree"]]
    changed = {k for k, v in head_files.items() if fake.trees["t0"].get(k) != v}
    assert changed == {"context/assets/serving/dimensions.yaml"}
    assert fake.pulls[0]["reviewers"] == ["rev-gh"] and fake.refs["main"] == "c0"


def test_merge_eligibility_requires_both_human_controls(real_warehouse: Settings, tmp_path: Path) -> None:
    fake, ps, wf, cfg, _ = setup(real_warehouse, tmp_path)
    ps.transition("cfp-wf", State.VALIDATING, actor="system.validator")
    ps.transition(
        "cfp-wf",
        State.AWAITING_REVIEW,
        actor="system.validator",
        updates={"validation_json": {"verdict": "AWAITING_REVIEW"}},
    )
    wf.open_pull_request("cfp-wf")
    ok, reasons = wf.check_merge_eligibility("cfp-wf")
    assert not ok and any("not APPROVED" in r for r in reasons)
    ps.decide("cfp-wf", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
    ok, reasons = wf.check_merge_eligibility("cfp-wf")
    assert not ok and reasons == ["no human GitHub approval on the current head commit"]
    head = fake.refs["groundwork/proposal-cfp-wf"]
    fake.reviews = [{"user": {"login": "rev-gh", "type": "User"}, "state": "APPROVED", "commit_id": head}]
    ok, reasons = wf.check_merge_eligibility("cfp-wf")
    assert ok and ps.get("cfp-wf").state == State.MERGE_ELIGIBLE  # type: ignore[union-attr]
    assert fake.pulls[0].get("merged") is None  # auto_merge is off: humans merge on GitHub


def test_push_to_pr_after_approval_invalidates_it(real_warehouse: Settings, tmp_path: Path) -> None:
    fake, ps, wf, cfg, _ = setup(real_warehouse, tmp_path)
    ps.transition("cfp-wf", State.VALIDATING, actor="system.validator")
    ps.transition(
        "cfp-wf",
        State.AWAITING_REVIEW,
        actor="system.validator",
        updates={"validation_json": {"verdict": "AWAITING_REVIEW"}},
    )
    wf.open_pull_request("cfp-wf")
    ps.decide("cfp-wf", decision="approve", slack_user_id="UREVIEW1", revision=1, config=cfg)
    fake.refs["groundwork/proposal-cfp-wf"] = "c-someone-else"  # an extra commit pushed to the PR branch
    fake.commits["c-someone-else"] = {"tree": "t0"}
    assert wf.sync("cfp-wf") == State.PROPOSED
    ok, _reasons = wf.check_merge_eligibility("cfp-wf")
    assert not ok


def test_pr_merged_outside_the_workflow_is_not_deployed(real_warehouse: Settings, tmp_path: Path) -> None:
    fake, ps, wf, _cfg, _ = setup(real_warehouse, tmp_path)
    ps.transition("cfp-wf", State.VALIDATING, actor="system.validator")
    ps.transition(
        "cfp-wf",
        State.AWAITING_REVIEW,
        actor="system.validator",
        updates={"validation_json": {"verdict": "AWAITING_REVIEW"}},
    )
    wf.open_pull_request("cfp-wf")
    fake.pulls[0]["merged"] = True  # merged by someone on GitHub without a Groundwork approval
    assert wf.sync("cfp-wf") == State.BLOCKED
    with pytest.raises(ValueError, match="MERGED"):
        wf.deploy_merged("cfp-wf", actor="ops")
