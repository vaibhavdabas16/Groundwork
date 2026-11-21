"""GitHub REST integration for context-fix pull requests.

Authentication (env only, never config files or logs):
* GitHub App (recommended): ``GITHUB_APP_ID``, ``GITHUB_APP_PRIVATE_KEY`` (PEM, or
  ``GITHUB_APP_PRIVATE_KEY_PATH``), ``GITHUB_APP_INSTALLATION_ID``. Installation tokens are requested
  with the minimum permissions ``contents: write`` + ``pull_requests: write`` (+ ``issues: write``
  for labels, ``checks: read`` / ``statuses: read`` for status) and restricted to ONE repository.
* Fine-grained personal access token: ``GROUNDWORK_GITHUB_TOKEN`` with the same repository-scoped
  permissions. Classic tokens are not recommended (they cannot be scoped to one repository).

Safety:
* Every file path is re-checked against the proposal allow-list (``check_repo_path``) before any
  blob is created. Protected paths can never be committed by this client.
* The branch name is derived from the stable proposal id; an existing PR for that head branch is
  reused, so retries never create duplicate PRs. Partial progress (branch/commit/PR) is persisted
  by the caller after each step, so an interrupted run resumes instead of repeating.
* This client has NO merge method unless ``allow_merge`` is set explicitly by the governance
  workflow, and even then ``merge_if_eligible`` re-checks human approval + checks + head SHA.
"""

from __future__ import annotations

import base64
import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

from groundwork.product.proposals.patches import check_repo_path

API_VERSION = "2022-11-28"
INSTALLATION_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "statuses": "read",
}


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class GitHubNotConfigured(GitHubError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"GitHub integration not configured; missing {missing}")
        self.missing = missing


@dataclass
class PullRequest:
    number: int
    url: str
    state: str  # open | closed
    merged: bool
    head_sha: str
    head_ref: str
    merge_commit_sha: str | None
    mergeable_state: str | None


class TokenSource:
    def token(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class StaticToken(TokenSource):
    def __init__(self, token: str) -> None:
        self._t = token

    def token(self) -> str:
        return self._t


class AppInstallationToken(TokenSource):
    """Mints short-lived installation tokens (JWT RS256 -> /app/installations/{id}/access_tokens)."""

    def __init__(
        self, app_id: str, private_key: str, installation_id: str, repo_name: str, client: httpx.Client
    ) -> None:
        self.app_id, self.key, self.installation_id, self.repo_name = (
            app_id,
            private_key,
            installation_id,
            repo_name,
        )
        self.client = client
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 120:
            return self._token
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256"
        )
        r = self.client.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={"repositories": [self.repo_name], "permissions": INSTALLATION_PERMISSIONS},
        )
        if r.status_code != 201:
            raise GitHubError(f"installation token request failed ({r.status_code})", status=r.status_code)
        body = r.json()
        self._token = str(body["token"])
        exp = body.get("expires_at")
        self._exp = time.time() + 3000 if not exp else _parse_ts(exp)
        return self._token


def _parse_ts(s: str) -> float:
    import datetime as dt

    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class GitHubClient:
    def __init__(
        self,
        repository: str,
        tokens: TokenSource | None = None,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        timeout: float = 20.0,
        allow_merge: bool = False,
    ) -> None:
        self.repository = repository
        self.http = httpx.Client(base_url=api_url, timeout=timeout, transport=transport)
        self.tokens = tokens
        self.max_retries = max_retries
        self.allow_merge = allow_merge

    @classmethod
    def from_env(
        cls,
        repository: str | None,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> GitHubClient:
        if not repository:
            raise GitHubNotConfigured(["github.repository (config/product.yaml)"])
        client = cls(repository, api_url=api_url, transport=transport)
        app_id = os.environ.get("GITHUB_APP_ID")
        if app_id:
            key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
            key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
            if not key and key_path:
                key = Path(key_path).read_text(encoding="utf-8")
            inst = os.environ.get("GITHUB_APP_INSTALLATION_ID")
            missing = [
                n for n, v in (("GITHUB_APP_PRIVATE_KEY", key), ("GITHUB_APP_INSTALLATION_ID", inst)) if not v
            ]
            if missing:
                raise GitHubNotConfigured(missing)
            assert key is not None and inst is not None
            client.tokens = AppInstallationToken(app_id, key, inst, repository.split("/", 1)[1], client.http)
        elif os.environ.get("GROUNDWORK_GITHUB_TOKEN"):
            client.tokens = StaticToken(os.environ["GROUNDWORK_GITHUB_TOKEN"])
        else:
            raise GitHubNotConfigured(["GITHUB_APP_ID (+key, installation) or GROUNDWORK_GITHUB_TOKEN"])
        return client

    # ---------------- transport ----------------

    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kw: Any) -> httpx.Response:
        if self.tokens is None:
            raise GitHubNotConfigured(["token"])
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self.tokens.token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "groundwork",
            }
            try:
                r = self.http.request(method, path, headers=headers, **kw)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise GitHubError(
                        f"GitHub connection error: {type(exc).__name__}", retryable=True
                    ) from exc
                attempt += 1
                time.sleep(min(2**attempt, 10))
                continue
            if r.status_code in ok:
                return r
            limited = r.status_code == 429 or (
                r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"
            )
            if (limited or r.status_code >= 500) and attempt < self.max_retries:
                attempt += 1
                wait = float(r.headers.get("retry-after") or 0)
                if not wait and r.headers.get("x-ratelimit-reset"):
                    wait = max(0.0, float(r.headers["x-ratelimit-reset"]) - time.time())
                time.sleep(min(wait or 2**attempt, 60))
                continue
            msg = ""
            with contextlib.suppress(ValueError):
                msg = str(r.json().get("message", ""))[:300]
            raise GitHubError(
                f"GitHub {method} {path} -> {r.status_code}: {msg}",
                status=r.status_code,
                retryable=limited or r.status_code >= 500,
            )

    @property
    def _repo(self) -> str:
        return f"/repos/{self.repository}"

    # ---------------- operations ----------------

    def branch_sha(self, branch: str) -> str | None:
        try:
            r = self._req("GET", f"{self._repo}/git/ref/heads/{branch}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return str(r.json()["object"]["sha"])

    def ensure_branch(self, branch: str, base: str) -> str:
        sha = self.branch_sha(branch)
        if sha:
            return sha
        base_sha = self.branch_sha(base)
        if base_sha is None:
            raise GitHubError(f"base branch {base} not found", status=404)
        try:
            self._req(
                "POST",
                f"{self._repo}/git/refs",
                ok=(201,),
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        except GitHubError as exc:
            if exc.status != 422:  # 422 = already exists (race with a concurrent retry)
                raise
        return self.branch_sha(branch) or base_sha

    def file_text(self, path: str, ref: str) -> str | None:
        try:
            r = self._req("GET", f"{self._repo}/contents/{path}", params={"ref": ref})
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return base64.b64decode(r.json()["content"]).decode("utf-8")

    def commit_files(self, branch: str, files: dict[str, str], message: str) -> str:
        """Commit all ``files`` (path -> full text) atomically on ``branch``. Returns the commit SHA.
        No-op (returns the head) when the branch already contains exactly this content."""
        for p in files:
            check_repo_path(p)
        head = self.branch_sha(branch)
        if head is None:
            raise GitHubError(f"branch {branch} does not exist", status=404)
        if all(self.file_text(p, branch) == text for p, text in files.items()):
            return head
        commit = self._req("GET", f"{self._repo}/git/commits/{head}").json()
        tree_items = []
        for p, text in files.items():
            blob = self._req(
                "POST", f"{self._repo}/git/blobs", ok=(201,), json={"content": text, "encoding": "utf-8"}
            ).json()
            tree_items.append({"path": p, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._req(
            "POST",
            f"{self._repo}/git/trees",
            ok=(201,),
            json={"base_tree": commit["tree"]["sha"], "tree": tree_items},
        ).json()
        new = self._req(
            "POST",
            f"{self._repo}/git/commits",
            ok=(201,),
            json={"message": message, "tree": tree["sha"], "parents": [head]},
        ).json()
        self._req("PATCH", f"{self._repo}/git/refs/heads/{branch}", json={"sha": new["sha"], "force": False})
        return str(new["sha"])

    def find_pr(self, branch: str) -> PullRequest | None:
        owner = self.repository.split("/", 1)[0]
        r = self._req(
            "GET", f"{self._repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all", "per_page": 10}
        )
        prs = r.json()
        if not prs:
            return None
        return self.get_pr(int(prs[0]["number"]))

    def get_pr(self, number: int) -> PullRequest:
        d = self._req("GET", f"{self._repo}/pulls/{number}").json()
        return PullRequest(
            number=int(d["number"]),
            url=str(d["html_url"]),
            state=str(d["state"]),
            merged=bool(d.get("merged")),
            head_sha=str(d["head"]["sha"]),
            head_ref=str(d["head"]["ref"]),
            merge_commit_sha=d.get("merge_commit_sha") if d.get("merged") else None,
            mergeable_state=d.get("mergeable_state"),
        )

    def open_pr(
        self, *, branch: str, base: str, title: str, body: str, labels: list[str], reviewers: list[str]
    ) -> PullRequest:
        existing = self.find_pr(branch)
        if existing is not None:
            if existing.state == "open":
                self._req(
                    "PATCH", f"{self._repo}/pulls/{existing.number}", json={"title": title, "body": body}
                )
            return self.get_pr(existing.number)
        d = self._req(
            "POST",
            f"{self._repo}/pulls",
            ok=(201,),
            json={"title": title, "head": branch, "base": base, "body": body, "draft": False},
        ).json()
        number = int(d["number"])
        if labels:
            self._req("POST", f"{self._repo}/issues/{number}/labels", json={"labels": labels})
        if reviewers:
            try:
                self._req(
                    "POST",
                    f"{self._repo}/pulls/{number}/requested_reviewers",
                    ok=(201,),
                    json={"reviewers": reviewers},
                )
            except GitHubError as exc:  # reviewer may lack repo access: PR still valid, reported
                if exc.status != 422:
                    raise
        return self.get_pr(number)

    def comment(self, number: int, body: str) -> None:
        self._req("POST", f"{self._repo}/issues/{number}/comments", ok=(201,), json={"body": body[:60000]})

    def close_pr(self, number: int) -> None:
        self._req("PATCH", f"{self._repo}/pulls/{number}", json={"state": "closed"})

    def checks_state(self, sha: str) -> str:
        """'success' only if the combined status AND every check run concluded successfully."""
        status = self._req("GET", f"{self._repo}/commits/{sha}/status").json()
        runs = self._req("GET", f"{self._repo}/commits/{sha}/check-runs", params={"per_page": 100}).json()
        conclusions = [cr.get("conclusion") for cr in runs.get("check_runs", [])]
        if any(c is None for c in conclusions):
            return "pending"
        if any(c not in ("success", "neutral", "skipped") for c in conclusions):
            return "failure"
        combined = status.get("state")
        if status.get("total_count", 0) and combined != "success":
            return str(combined)
        return "success" if conclusions or status.get("total_count", 0) else "none"

    def human_approved(self, number: int, head_sha: str) -> list[str]:
        """Logins of GitHub users whose LATEST review is APPROVED on the current head commit."""
        reviews = self._req("GET", f"{self._repo}/pulls/{number}/reviews", params={"per_page": 100}).json()
        latest: dict[str, dict[str, Any]] = {}
        for rv in reviews:
            user = rv.get("user") or {}
            if user.get("type") == "Bot":
                continue
            latest[str(user.get("login"))] = rv
        return [
            u for u, rv in latest.items() if rv.get("state") == "APPROVED" and rv.get("commit_id") == head_sha
        ]

    def merge(self, number: int, head_sha: str, title: str) -> str:
        if not self.allow_merge:
            raise GitHubError("merging is disabled for this client (governance: humans merge)")
        d = self._req(
            "PUT",
            f"{self._repo}/pulls/{number}/merge",
            json={"sha": head_sha, "merge_method": "squash", "commit_title": title},
        ).json()
        return str(d["sha"])
