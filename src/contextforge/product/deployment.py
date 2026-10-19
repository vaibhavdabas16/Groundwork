"""Context releases: the running agent consumes ONLY an explicitly deployed, validated version.

Layout (``data/context_releases``)::

    releases/<context_version>/context/...   immutable copy of a validated context tree
    CURRENT.json                              {"version", "deployment_id", "content_hash"}

* ``deploy`` validates the candidate (full ``validate_directory`` + strict service load), copies it to
  an immutable release directory, then switches ``CURRENT.json`` atomically (write temp + ``os.replace``).
  Requests started after the switch use the new version; in-flight requests keep the version they
  started with (each request starts its own MCP server against the release path it resolved).
* An invalid candidate is REJECTED: nothing is switched, the last valid release stays active, the
  rejection is recorded and audited, and an alert callback fires.
* ``rollback`` re-activates a previous release (which was validated when deployed) and re-verifies it.
* Unreviewed PR content never becomes production context: deploy sources are a local directory the
  operator chose or a git commit that the caller verifies is on the protected base branch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contextforge.config.settings import ContextMode, Settings
from contextforge.context.loader import load_context
from contextforge.context.validation import errors, validate_directory
from contextforge.product.audit import AuditLog
from contextforge.product.db import Database


class DeploymentRejected(RuntimeError):
    def __init__(self, message: str, problems: list[str]) -> None:
        super().__init__(message)
        self.problems = problems


@dataclass(frozen=True)
class ActiveRelease:
    version: str
    deployment_id: str
    content_hash: str
    context_dir: Path


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def tree_hash(context_dir: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in context_dir.rglob("*") if p.is_file()):
        h.update(f.relative_to(context_dir).as_posix().encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


class ContextDeployer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        audit: AuditLog,
        *,
        releases_root: Path | None = None,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self.root = releases_root or settings.data_dir / "context_releases"
        self.alert = alert or (lambda _msg: None)

    @property
    def pointer(self) -> Path:
        return self.root / "CURRENT.json"

    def active(self) -> ActiveRelease | None:
        if not self.pointer.is_file():
            return None
        d = json.loads(self.pointer.read_text(encoding="utf-8"))
        ctx = self.root / "releases" / d["version"] / "context"
        return ActiveRelease(d["version"], d["deployment_id"], d["content_hash"], ctx)

    def context_dir_for_requests(self) -> tuple[Path, str]:
        """(context dir, provenance label). Falls back to the working tree ONLY when no release
        exists (development); health reports this as not production-ready."""
        rel = self.active()
        if rel is None:
            return self.settings.context_dir, "working-tree (no release deployed)"
        return rel.context_dir, f"release {rel.version}"

    def _check(self, context_dir: Path) -> list[str]:
        problems: list[str] = []
        try:
            _, issues = validate_directory(context_dir, self.settings.limits)
            problems += [str(e) for e in errors(issues)]
        except Exception as exc:
            problems.append(f"context failed to load: {type(exc).__name__}: {exc}")
        if not problems:
            from contextforge.service.core import ContextForgeService

            try:
                svc = ContextForgeService(
                    self.settings.model_copy(update={"context_dir": context_dir}),
                    context_mode=ContextMode.full_context,
                )
                env = svc.search_assets("orders")
                if not env.ok:
                    problems.append("health probe search_assets('orders') failed")
            except Exception as exc:
                problems.append(f"service failed to start on candidate: {type(exc).__name__}: {exc}")
        return problems

    def deploy(
        self,
        source_dir: Path,
        *,
        actor: str,
        actor_type: str = "human",
        source_label: str | None = None,
        git_commit: str | None = None,
        proposal_id: str | None = None,
    ) -> ActiveRelease:
        problems = self._check(source_dir)
        store = None if problems else load_context(source_dir)
        did = f"dep-{uuid.uuid4().hex[:10]}"
        version = store.version if store else "invalid"
        chash = tree_hash(source_dir) if source_dir.is_dir() else ""
        prev = self.active()
        if problems:
            with self.db.tx() as c:
                c.execute(
                    "INSERT INTO context_deployments VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        did,
                        version,
                        chash,
                        source_label or str(source_dir),
                        git_commit,
                        proposal_id,
                        "REJECTED",
                        json.dumps(problems[:50]),
                        actor,
                        _now(),
                    ),
                )
                self.audit.append(
                    action="context.deploy_rejected",
                    actor_id=actor,
                    actor_type=actor_type,  # type: ignore[arg-type]
                    resource_type="context_release",
                    resource_id=did,
                    prior_state=prev.version if prev else None,
                    new_state=prev.version if prev else None,
                    context_version=version,
                    details={"problems": problems[:20], "source": source_label or str(source_dir)},
                    conn=c,
                )
            self.alert(
                f"Context deployment REJECTED ({len(problems)} problems); still serving {prev.version if prev else 'no release'}"
            )
            raise DeploymentRejected("candidate context is invalid; deployment rejected", problems)
        assert store is not None
        dest = self.root / "releases" / version / "context"
        if not dest.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=self.root, prefix=".staging-"))
            try:
                shutil.copytree(source_dir, tmp / "context")
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp / "context", dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if tree_hash(dest) != chash:
            raise DeploymentRejected(
                "release directory content differs from the validated source", ["hash mismatch"]
            )
        self._switch(version, did, chash)
        with self.db.tx() as c:
            c.execute("UPDATE context_deployments SET status = 'SUPERSEDED' WHERE status = 'ACTIVE'")
            c.execute(
                "INSERT INTO context_deployments VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    version,
                    chash,
                    source_label or str(source_dir),
                    git_commit,
                    proposal_id,
                    "ACTIVE",
                    json.dumps({"validated": True}),
                    actor,
                    _now(),
                ),
            )
            self.audit.append(
                action="context.deployed",
                actor_id=actor,
                actor_type=actor_type,  # type: ignore[arg-type]
                resource_type="context_release",
                resource_id=did,
                prior_state=prev.version if prev else None,
                new_state=version,
                github_ref=git_commit,
                context_version=version,
                details={"content_hash": chash, "proposal_id": proposal_id},
                conn=c,
            )
        active = self.verify()
        if active.version != version:
            raise DeploymentRejected(
                "post-deploy verification failed", [f"active={active.version} expected={version}"]
            )
        return active

    def _switch(self, version: str, deployment_id: str, chash: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"version": version, "deployment_id": deployment_id, "content_hash": chash}),
            encoding="utf-8",
        )
        os.replace(tmp, self.pointer)

    def verify(self) -> ActiveRelease:
        rel = self.active()
        if rel is None:
            raise DeploymentRejected("no active release", ["no CURRENT.json"])
        if tree_hash(rel.context_dir) != rel.content_hash:
            raise DeploymentRejected("active release content hash mismatch", ["modified release directory"])
        if load_context(rel.context_dir).version != rel.version:
            raise DeploymentRejected("active release version mismatch", ["version"])
        return rel

    def rollback(self, *, actor: str, to_version: str | None = None) -> ActiveRelease:
        cur = self.active()
        rows = self.db.query(
            "SELECT * FROM context_deployments WHERE status IN ('SUPERSEDED','ROLLED_BACK','ACTIVE') ORDER BY created_at DESC"
        )
        target = None
        for r in rows:
            if (to_version and r["context_version"] == to_version) or (
                not to_version and cur and r["context_version"] != cur.version
            ):
                target = dict(r)
                break
        if target is None:
            raise DeploymentRejected("no previous release to roll back to", [])
        dest = self.root / "releases" / target["context_version"] / "context"
        problems = self._check(dest)
        if problems:
            raise DeploymentRejected("rollback target no longer validates", problems)
        self._switch(target["context_version"], target["deployment_id"], target["content_hash"])
        with self.db.tx() as c:
            c.execute("UPDATE context_deployments SET status = 'ROLLED_BACK' WHERE status = 'ACTIVE'")
            c.execute(
                "UPDATE context_deployments SET status = 'ACTIVE' WHERE deployment_id = ?",
                (target["deployment_id"],),
            )
            self.audit.append(
                action="context.rollback",
                actor_id=actor,
                actor_type="human",
                resource_type="context_release",
                resource_id=target["deployment_id"],
                prior_state=cur.version if cur else None,
                new_state=target["context_version"],
                context_version=target["context_version"],
                details={},
                conn=c,
            )
        return self.verify()

    def deploy_git_commit(
        self, repo_root: Path, commit: str, *, base_branch: str, actor: str, proposal_id: str | None = None
    ) -> ActiveRelease:
        """Deploy ``context/`` from a commit that MUST be reachable from ``origin/<base_branch>``
        (i.e. merged through the protected branch), never from an unmerged PR head."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", base_branch
        ):
            raise DeploymentRejected("commit must be a hex SHA and the base branch a plain name", [])
        git = shutil.which("git")
        if git is None:
            raise DeploymentRejected("git is not installed", [])
        subprocess.run([git, "fetch", "origin", base_branch], cwd=repo_root, check=True, capture_output=True)  # noqa: S603
        ok = subprocess.run(  # noqa: S603
            [git, "merge-base", "--is-ancestor", commit, f"origin/{base_branch}"],
            cwd=repo_root,
            capture_output=True,
        )
        if ok.returncode != 0:
            raise DeploymentRejected(
                f"commit {commit} is not on origin/{base_branch}; refusing to deploy", []
            )
        with tempfile.TemporaryDirectory() as t:
            archive = subprocess.run(  # noqa: S603 - fixed argv; commit validated as a hex SHA
                [git, "archive", commit, "context"], cwd=repo_root, check=True, capture_output=True
            )
            tar = Path(t) / "ctx.tar"
            tar.write_bytes(archive.stdout)
            shutil.unpack_archive(tar, t, filter="data")
            return self.deploy(
                Path(t) / "context",
                actor=actor,
                source_label=f"git:{commit}",
                git_commit=commit,
                proposal_id=proposal_id,
            )

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM context_deployments ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        ]
