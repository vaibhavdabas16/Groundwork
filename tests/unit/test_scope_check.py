"""The CI proposal-scope guard against a real temporary git repository."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from groundwork.product.scope_check import main

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [GIT or "git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        ),
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    (r / "context" / "glossary").mkdir(parents=True)
    (r / "context" / "glossary" / "terms.yaml").write_text("items: []\n")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("x = 1\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "groundwork/proposal-x")
    monkeypatch.chdir(r)
    return r


def test_allowlisted_context_change_passes(repo: Path) -> None:
    (repo / "context" / "glossary" / "terms.yaml").write_text(
        "items:\n  - id: term.x\n    certification: {status: draft}\n"
    )
    _git(repo, "commit", "-qam", "proposal")
    assert main(["--base", "main"]) == 0


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("src/app.py", "x = 2\n"),
        (".github/workflows/ci.yml", "on: push\n"),
        ("evals/expected/cf-001.json", "{}\n"),
    ],
)
def test_code_ci_or_gold_changes_fail(repo: Path, path: str, content: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sneaky")
    assert main(["--base", "main"]) == 1


def test_self_certification_fails(repo: Path) -> None:
    (repo / "context" / "glossary" / "terms.yaml").write_text(
        "items:\n  - id: term.x\n    certification: {status: verified, certified_by: agent.claude_code}\n"
    )
    _git(repo, "commit", "-qam", "certify")
    assert main(["--base", "main"]) == 1
