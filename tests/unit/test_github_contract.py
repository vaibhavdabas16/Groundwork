"""GitHub integration CONTRACT tests against an in-memory fake of the GitHub REST endpoints we use
(httpx MockTransport; no network). They prove request shapes, idempotency, path enforcement, retry
and error handling. They are NOT evidence that a real repository accepted a PR."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from groundwork.product.github import AppInstallationToken, GitHubClient, GitHubError, StaticToken
from groundwork.product.proposals.patches import PatchError

REPO = "acme/context"


class FakeGitHub:
    def __init__(self) -> None:
        self.refs = {"main": "c0"}
        self.commits: dict[str, dict[str, Any]] = {
            "c0": {"tree": "t0", "files": {"context/glossary/terms.yaml": "old\n"}}
        }
        self.pulls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.blobs: dict[str, str] = {}
        self.trees: dict[str, dict[str, str]] = {"t0": dict(self.commits["c0"]["files"])}
        self.fail_next: list[int] = []
        self.reviews: list[dict[str, Any]] = []
        self.auth_seen: list[str] = []
        self.token_requests: list[dict[str, Any]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        self.auth_seen.append(req.headers.get("authorization", ""))
        if self.fail_next:
            code = self.fail_next.pop(0)
            return httpx.Response(code, json={"message": "fail"}, headers={"retry-after": "0"})
        body = json.loads(req.content) if req.content else {}
        p = path.removeprefix(f"/repos/{REPO}")
        if path.startswith("/app/installations/"):
            self.token_requests.append(body)
            return httpx.Response(
                201, json={"token": "ghs_installation", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if p.startswith("/git/ref/heads/"):
            b = p.split("/git/ref/heads/")[1]
            return (
                httpx.Response(200, json={"object": {"sha": self.refs[b]}})
                if b in self.refs
                else httpx.Response(404, json={"message": "Not Found"})
            )
        if p == "/git/refs" and req.method == "POST":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={})
        if p.startswith("/contents/"):
            f = p.removeprefix("/contents/")
            files = self.trees[self.commits[self.refs[req.url.params["ref"]]]["tree"]]
            if f not in files:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"content": base64.b64encode(files[f].encode()).decode()})
        if p.startswith("/git/commits/") and req.method == "GET":
            sha = p.split("/")[-1]
            return httpx.Response(200, json={"tree": {"sha": self.commits[sha]["tree"]}})
        if p == "/git/blobs":
            sha = f"b{len(self.blobs)}"
            self.blobs[sha] = body["content"]
            return httpx.Response(201, json={"sha": sha})
        if p == "/git/trees":
            t = dict(self.trees[body["base_tree"]])
            for it in body["tree"]:
                t[it["path"]] = self.blobs[it["sha"]]
            sha = f"t{len(self.trees)}"
            self.trees[sha] = t
            return httpx.Response(201, json={"sha": sha})
        if p == "/git/commits" and req.method == "POST":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = {"tree": body["tree"]}
            return httpx.Response(201, json={"sha": sha})
        if p.startswith("/git/refs/heads/") and req.method == "PATCH":
            self.refs[p.split("/git/refs/heads/")[1]] = body["sha"]
            return httpx.Response(200, json={})
        if p == "/pulls" and req.method == "GET":
            head = req.url.params["head"].split(":")[1]
            return httpx.Response(
                200, json=[{"number": pr["number"]} for pr in self.pulls if pr["head"] == head]
            )
        if p == "/pulls" and req.method == "POST":
            n = len(self.pulls) + 1
            self.pulls.append(
                {
                    "number": n,
                    "head": body["head"],
                    "title": body["title"],
                    "body": body["body"],
                    "state": "open",
                    "labels": [],
                    "reviewers": [],
                }
            )
            return httpx.Response(201, json={"number": n})
        if p.startswith("/pulls/") and p.count("/") == 2:
            n = int(p.split("/")[2])
            pr = self.pulls[n - 1]
            if req.method == "PATCH":
                pr.update({k: v for k, v in body.items() if k in ("title", "body", "state")})
            return httpx.Response(
                200,
                json={
                    "number": n,
                    "html_url": f"https://github.com/{REPO}/pull/{n}",
                    "state": pr["state"],
                    "merged": pr.get("merged", False),
                    "head": {"sha": self.refs[pr["head"]], "ref": pr["head"]},
                    "merge_commit_sha": None,
                    "mergeable_state": "clean",
                },
            )
        if p.endswith("/labels"):
            self.pulls[int(p.split("/")[2]) - 1]["labels"] = body["labels"]
            return httpx.Response(200, json=[])
        if p.endswith("/requested_reviewers"):
            self.pulls[int(p.split("/")[2]) - 1]["reviewers"] = body["reviewers"]
            return httpx.Response(201, json={})
        if p.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews)
        if p.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "total_count": 1})
        if p.endswith("/check-runs"):
            return httpx.Response(
                200, json={"check_runs": [{"conclusion": "success"}, {"conclusion": "success"}]}
            )
        return httpx.Response(404, json={"message": f"unhandled {req.method} {path}"})


def client(fake: FakeGitHub, **kw: Any) -> GitHubClient:
    return GitHubClient(
        REPO, StaticToken("github_pat_test"), transport=httpx.MockTransport(fake), max_retries=2, **kw
    )


def test_branch_commit_and_pr_are_idempotent() -> None:
    fake = FakeGitHub()
    gh = client(fake)
    branch = "groundwork/proposal-cfp-1"
    gh.ensure_branch(branch, "main")
    gh.ensure_branch(branch, "main")  # retry: no second ref creation
    assert sum(1 for c in fake.calls if c == ("POST", f"/repos/{REPO}/git/refs")) == 1
    sha = gh.commit_files(branch, {"context/glossary/terms.yaml": "new\n"}, "msg")
    assert fake.trees[fake.commits[sha]["tree"]]["context/glossary/terms.yaml"] == "new\n"
    assert (
        gh.commit_files(branch, {"context/glossary/terms.yaml": "new\n"}, "msg") == sha
    )  # identical content: no new commit
    pr1 = gh.open_pr(
        branch=branch, base="main", title="t", body="b", labels=["context-change"], reviewers=["sam"]
    )
    pr2 = gh.open_pr(
        branch=branch, base="main", title="t2", body="b2", labels=["context-change"], reviewers=["sam"]
    )
    assert pr1.number == pr2.number == 1 and len(fake.pulls) == 1
    assert fake.pulls[0]["labels"] == ["context-change"] and fake.pulls[0]["reviewers"] == ["sam"]
    assert fake.refs["main"] == "c0"  # base branch never written
    assert all(a == "Bearer github_pat_test" for a in fake.auth_seen)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "src/groundwork/eval/scoring.py",
        "evals/expected/cf-001.json",
        "context/policies/policy.yaml",
        "context/ownership/owners.yaml",
        "pyproject.toml",
        "../context/x.yaml",
    ],
)
def test_protected_paths_are_refused_before_any_request(path: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(PatchError):
        client(fake).commit_files("main", {path: "x"}, "msg")
    assert fake.calls == []


def test_rate_limit_and_server_errors_retry_then_surface() -> None:
    fake = FakeGitHub()
    fake.fail_next = [429, 502]
    assert client(fake).branch_sha("main") == "c0"
    fake.fail_next = [503, 503, 503]
    with pytest.raises(GitHubError) as ei:
        client(fake).branch_sha("main")
    assert ei.value.retryable and ei.value.status == 503


def test_expired_or_invalid_token_is_not_retried() -> None:
    fake = FakeGitHub()
    fake.fail_next = [401]
    with pytest.raises(GitHubError) as ei:
        client(fake).branch_sha("main")
    assert ei.value.status == 401 and not ei.value.retryable and len(fake.calls) == 1


def test_merge_is_disabled_unless_governance_enables_it() -> None:
    with pytest.raises(GitHubError, match="disabled"):
        client(FakeGitHub()).merge(1, "sha", "t")


def test_checks_and_human_reviews_on_current_head_only() -> None:
    fake = FakeGitHub()
    gh = client(fake)
    assert gh.checks_state("c0") == "success"
    fake.reviews = [
        {"user": {"login": "dependabot", "type": "Bot"}, "state": "APPROVED", "commit_id": "c9"},
        {"user": {"login": "sam", "type": "User"}, "state": "APPROVED", "commit_id": "old"},
        {"user": {"login": "kim", "type": "User"}, "state": "APPROVED", "commit_id": "c9"},
        {"user": {"login": "lee", "type": "User"}, "state": "APPROVED", "commit_id": "c9"},
        {"user": {"login": "lee", "type": "User"}, "state": "CHANGES_REQUESTED", "commit_id": "c9"},
    ]
    assert gh.human_approved(1, "c9") == ["kim"]


def test_app_installation_token_is_repository_scoped_with_minimal_permissions() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    fake = FakeGitHub()
    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(fake))
    src = AppInstallationToken("12345", pem, "678", "context", http)
    assert src.token() == "ghs_installation" and src.token() == "ghs_installation"
    assert len(fake.token_requests) == 1  # cached until near expiry
    req = fake.token_requests[0]
    assert req["repositories"] == ["context"]
    assert req["permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",
        "statuses": "read",
    }
    claims = jwt.decode(fake.auth_seen[0].removeprefix("Bearer "), key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
