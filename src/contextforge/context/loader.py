"""Load the Git-versioned context repository (``context/``) into typed, indexed objects."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from contextforge.common.hashing import sha256_text
from contextforge.context.models import (
    Asset,
    GlossaryTerm,
    JoinRelationship,
    Metric,
    Owner,
    Policy,
    UnsupportedConcept,
)

# directory -> model
SECTIONS: dict[str, type[BaseModel]] = {
    "assets": Asset,
    "glossary": GlossaryTerm,
    "metrics": Metric,
    "joins": JoinRelationship,
    "ownership": Owner,
    "unsupported": UnsupportedConcept,
    "policies": Policy,
}


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    file: str | None = None
    object_id: str | None = None

    def __str__(self) -> str:
        loc = f"{self.file}" + (f" [{self.object_id}]" if self.object_id else "") if self.file else ""
        return f"{self.severity.upper()} {self.code}: {self.message}" + (f" ({loc})" if loc else "")


@dataclass
class ContextStore:
    root: Path
    version: str
    content_hash: str
    git_commit: str | None
    assets: dict[str, Asset] = field(default_factory=dict)
    terms: dict[str, GlossaryTerm] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    joins: dict[str, JoinRelationship] = field(default_factory=dict)
    owners: dict[str, Owner] = field(default_factory=dict)
    unsupported: dict[str, UnsupportedConcept] = field(default_factory=dict)
    policies: dict[str, Policy] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)  # object id -> relative file path
    load_issues: list[Issue] = field(default_factory=list)

    @property
    def policy(self) -> Policy:
        if len(self.policies) != 1:
            raise RuntimeError(f"expected exactly one policy document, found {len(self.policies)}")
        return next(iter(self.policies.values()))

    def all_ids(self) -> Iterator[str]:
        for d in (
            self.assets,
            self.terms,
            self.metrics,
            self.joins,
            self.owners,
            self.unsupported,
            self.policies,
        ):
            yield from d

    def asset_by_qualified_name(self, qname: str) -> Asset | None:
        q = qname.lower()
        return next((a for a in self.assets.values() if a.qualified_name.lower() == q), None)

    def kind_of(self, object_id: str) -> str | None:
        for kind, d in (
            ("asset", self.assets),
            ("glossary_term", self.terms),
            ("metric", self.metrics),
            ("join", self.joins),
            ("owner", self.owners),
            ("unsupported_concept", self.unsupported),
            ("policy", self.policies),
        ):
            if object_id in d:
                return kind
        return None


_TARGET: dict[type[BaseModel], str] = {
    Asset: "assets",
    GlossaryTerm: "terms",
    Metric: "metrics",
    JoinRelationship: "joins",
    Owner: "owners",
    UnsupportedConcept: "unsupported",
    Policy: "policies",
}


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "log", "-1", "--format=%h", "--", "."],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _documents(raw: Any) -> list[Any]:
    if isinstance(raw, dict) and set(raw) == {"items"} and isinstance(raw["items"], list):
        return list(raw["items"])
    return [raw]


def load_context(root: Path) -> ContextStore:
    """Parse all YAML documents. Schema errors are collected as issues, not raised."""
    files = sorted(p for p in root.rglob("*.y*ml") if p.is_file())
    hasher_input = []
    issues: list[Issue] = []
    store = ContextStore(root=root, version="", content_hash="", git_commit=_git_commit(root))
    seen: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        section = rel.split("/", 1)[0]
        model = SECTIONS.get(section)
        text = path.read_text(encoding="utf-8")
        hasher_input.append(f"{rel}\n{text}")
        if model is None:
            issues.append(
                Issue("error", "unknown_section", f"file is not in a known section {sorted(SECTIONS)}", rel)
            )
            continue
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            issues.append(Issue("error", "yaml_error", str(exc).splitlines()[0], rel))
            continue
        for i, doc in enumerate(_documents(raw)):
            doc_id = doc.get("id") if isinstance(doc, dict) else None
            try:
                obj = model.model_validate(doc)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(x) for x in err["loc"])
                    issues.append(
                        Issue("error", "schema_error", f"item {i}: {loc}: {err['msg']}", rel, doc_id)
                    )
                continue
            oid: str = obj.id  # type: ignore[attr-defined]
            if oid in seen:
                issues.append(
                    Issue("error", "duplicate_id", f"id {oid} already defined in {seen[oid]}", rel, oid)
                )
                continue
            seen[oid] = rel
            getattr(store, _TARGET[model])[oid] = obj
            store.source_files[oid] = f"context/{rel}"
    store.content_hash = sha256_text("\n".join(hasher_input))
    store.version = f"ctx-{store.content_hash[:12]}" + (
        f"+git.{store.git_commit}" if store.git_commit else ""
    )
    store.load_issues = issues
    return store
