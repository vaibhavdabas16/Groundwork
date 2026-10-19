"""LIVE Slack and GitHub integration checks (real API calls). Run in a trusted environment only:
``uv run pytest -m live tests/live/test_live_integrations.py``. Each test SKIPS with reason
"BLOCKED" when its credentials are absent; a skip is never a pass.

* Slack: ``SLACK_BOT_TOKEN`` + ``CONTEXTFORGE_LIVE_SLACK_CHANNEL`` (a test channel the bot is in).
* GitHub: credentials as in ``product/github.py`` + ``CONTEXTFORGE_LIVE_GITHUB_REPO`` (a SANDBOX
  repository; the test creates a branch + PR touching only an allow-listed file, then closes it).
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = [pytest.mark.live]


def test_live_slack_auth_and_post() -> None:
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("CONTEXTFORGE_LIVE_SLACK_CHANNEL")
    if not token or not channel:
        pytest.skip("BLOCKED: SLACK_BOT_TOKEN / CONTEXTFORGE_LIVE_SLACK_CHANNEL not set")
    from contextforge.product.slack_app import BoltGateway, web_client

    client = web_client(token)
    auth = client.auth_test()
    assert auth["ok"] and auth["user_id"]
    ts = BoltGateway(client).post(channel, text="ContextForge live integration check (safe to ignore).")
    assert ts


def test_live_github_branch_pr_and_close() -> None:
    repo = os.environ.get("CONTEXTFORGE_LIVE_GITHUB_REPO")
    if not repo or not (os.environ.get("GITHUB_APP_ID") or os.environ.get("CONTEXTFORGE_GITHUB_TOKEN")):
        pytest.skip("BLOCKED: GitHub credentials / CONTEXTFORGE_LIVE_GITHUB_REPO not set")
    from contextforge.product.github import GitHubClient

    gh = GitHubClient.from_env(repo)
    branch = f"contextforge/proposal-livecheck-{int(time.time())}"
    gh.ensure_branch(branch, os.environ.get("CONTEXTFORGE_LIVE_GITHUB_BASE", "main"))
    sha = gh.commit_files(
        branch, {"context/glossary/livecheck.yaml": "items: []\n"}, "live integration check"
    )
    pr = gh.open_pr(
        branch=branch,
        base=os.environ.get("CONTEXTFORGE_LIVE_GITHUB_BASE", "main"),
        title="[ContextForge] live check",
        body="Automated live integration check; will be closed.",
        labels=[],
        reviewers=[],
    )
    assert pr.head_sha == sha and pr.state == "open"
    assert gh.find_pr(branch).number == pr.number  # type: ignore[union-attr]
    gh.close_pr(pr.number)
    assert gh.get_pr(pr.number).state == "closed"
