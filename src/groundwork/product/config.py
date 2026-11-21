"""Product configuration (``config/product.yaml``; path override ``GROUNDWORK_PRODUCT_CONFIG``).

This file holds IDENTITY and AUTHORIZATION mappings (Slack user -> policy principal, reviewer ->
human owner). It lives outside ``context/`` on purpose: the proposal system can only ever modify
allow-listed context files, so it can never grant itself users or reviewers.

Validation at load (``load_product_config``) fails closed:
* reviewers must map to a ``kind: human``, non-placeholder owner in ``context/ownership/owners.yaml``;
* pilot users must map to a principal that exists in the policy;
* nobody can be both a context reviewer and the configured proposal author identity.
Secrets (tokens) are NEVER read from this file; they come only from environment variables.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from groundwork.context.loader import ContextStore

SlackId = str


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PilotUser(Strict):
    slack_user_id: SlackId = Field(pattern=r"^[UW][A-Z0-9]{2,}$")
    principal: str = "principal.local_analyst"
    label: str | None = None  # non-identifying label for reports (e.g. "pilot-03")


class ContextReviewer(Strict):
    slack_user_id: SlackId = Field(pattern=r"^[UW][A-Z0-9]{2,}$")
    owner_id: str  # human owner in owners.yaml
    domains: list[str] = Field(default_factory=lambda: ["*"])  # fnmatch patterns over asset/metric domains
    elevated: bool = False  # may approve metric SQL changes / deprecations (elevated tier)
    github_login: str | None = None


class AnswerReviewer(Strict):
    slack_user_id: SlackId = Field(pattern=r"^[UW][A-Z0-9]{2,}$")
    owner_id: str


class SlackSettings(Strict):
    allowed_team_ids: list[str] = Field(default_factory=list)  # empty => refuse every event
    review_channel_id: str | None = None  # where proposal + shadow review requests are posted
    notify_channel_id: str | None = None  # operational alerts (deploy rejected, regressions)


class GitHubSettings(Strict):
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = "main"
    branch_prefix: str = "groundwork/proposal-"
    api_url: str = "https://api.github.com"
    labels: list[str] = Field(default_factory=lambda: ["context-change", "ai-proposed"])
    # Governance: humans merge on GitHub by default. auto_merge only merges a PR after an authorized
    # Slack approval of the current revision AND passing checks AND (if required) a GitHub review.
    auto_merge: bool = False
    require_github_approval: bool = True


class ProductConfig(Strict):
    delivery_mode: Literal["shadow", "live"] = "shadow"  # shadow: answers held for human review
    consent_notice_version: str = "pilot-notice-v1"
    require_consent: bool = True
    conversation_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    max_clarification_turns: int = Field(default=2, ge=1, le=5)
    max_concurrent_requests: int = Field(default=2, ge=1, le=16)
    per_user_requests_per_hour: int = Field(default=30, ge=1)
    proposal_author_id: str = "agent.groundwork_improver"
    users: list[PilotUser] = Field(default_factory=list)
    answer_reviewers: list[AnswerReviewer] = Field(default_factory=list)
    context_reviewers: list[ContextReviewer] = Field(default_factory=list)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    retention_days: int = Field(default=90, ge=1)

    def user(self, slack_user_id: str) -> PilotUser | None:
        return next((u for u in self.users if u.slack_user_id == slack_user_id), None)

    def answer_reviewer(self, slack_user_id: str) -> AnswerReviewer | None:
        return next((r for r in self.answer_reviewers if r.slack_user_id == slack_user_id), None)

    def context_reviewer(self, slack_user_id: str) -> ContextReviewer | None:
        return next((r for r in self.context_reviewers if r.slack_user_id == slack_user_id), None)

    def reviewers_for(self, domains: list[str], *, elevated: bool) -> list[ContextReviewer]:
        return [r for r in self.context_reviewers if reviewer_covers(r, domains, elevated=elevated)]


def reviewer_covers(r: ContextReviewer, domains: list[str], *, elevated: bool) -> bool:
    if elevated and not r.elevated:
        return False
    if not domains:
        return "*" in r.domains
    return all(any(fnmatch.fnmatch(d, pat) for pat in r.domains) for d in domains)


class ProductConfigError(ValueError):
    pass


def default_path(root: Path) -> Path:
    return Path(os.environ.get("GROUNDWORK_PRODUCT_CONFIG", root / "config" / "product.yaml"))


def load_product_config(path: Path, store: ContextStore | None = None) -> ProductConfig:
    if not path.is_file():
        raise ProductConfigError(f"product config not found: {path} (copy config/product.example.yaml)")
    try:
        cfg = ProductConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except ValidationError as exc:
        raise ProductConfigError(f"invalid product config {path}: {exc}") from exc
    if store is not None:
        problems = check_against_context(cfg, store)
        if problems:
            raise ProductConfigError("; ".join(problems))
    return cfg


def check_against_context(cfg: ProductConfig, store: ContextStore) -> list[str]:
    problems: list[str] = []
    principals = {p.id for p in store.policy.principals}
    for u in cfg.users:
        if u.principal not in principals:
            problems.append(f"pilot user {u.slack_user_id}: unknown principal {u.principal}")
    reviewers: list[ContextReviewer | AnswerReviewer] = [*cfg.context_reviewers, *cfg.answer_reviewers]
    for r in reviewers:
        owner = store.owners.get(r.owner_id)
        if owner is None:
            problems.append(f"reviewer {r.slack_user_id}: owner {r.owner_id} not in owners.yaml")
        elif owner.kind != "human" or owner.placeholder:
            problems.append(f"reviewer {r.slack_user_id}: owner {r.owner_id} is not a non-placeholder human")
    if cfg.proposal_author_id in {r.owner_id for r in cfg.context_reviewers}:
        problems.append("the proposal author identity cannot also be a context reviewer")
    ids = [r.slack_user_id for r in cfg.context_reviewers]
    if len(ids) != len(set(ids)):
        problems.append("duplicate context reviewer slack_user_id")
    return problems
