"""Apply structured proposal operations to a STAGED COPY of the context repository.

Security properties (each unit-tested):
* Only allow-listed files can change (``ALLOWED_PATTERNS``). Ownership, policy, raw-layer docs,
  evaluation gold, code, CI and dependency files are never writable by a proposal, regardless of
  what an LLM asks for. The path check is repeated by the GitHub layer before any commit and by
  the ``proposal-scope`` CI job on the PR itself (defence in depth).
* New and modified objects are forced to ``certification.status: draft`` with AI provenance;
  certification fields (``certified_by``/``certified_at``/``decision_ref``) can never be set.
* References are checked against the governed context AND the real serving-warehouse schema
  (read-only ``information_schema`` introspection by trusted code; no data rows are read).
* The staged repository must pass full ``validate_directory`` before a proposal is accepted.
* YAML is edited round-trip (comments and formatting preserved) so diffs stay minimal.
"""

from __future__ import annotations

import datetime as dt
import difflib
import fnmatch
import hashlib
import io
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import duckdb
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from contextforge.context.loader import ContextStore, load_context
from contextforge.context.validation import errors, validate_directory
from contextforge.product.proposals.models import (
    ELEVATED_OPS,
    AddGlossaryTerm,
    AddJoin,
    AddMetricCaveat,
    AddTermSynonym,
    AddUnsupportedConcept,
    AppendTermNote,
    ChangeMetricSql,
    DeprecateObject,
    FileDiff,
    PatchOp,
    SetColumnDescription,
    SetFanoutWarning,
)

# Repository-relative paths a proposal may modify. Everything else is forbidden.
ALLOWED_PATTERNS = (
    "context/glossary/*.yaml",
    "context/assets/serving/*.yaml",
    "context/joins/*.yaml",
    "context/metrics/*.yaml",
    "context/unsupported/*.yaml",
)
# Explicitly forbidden even if a pattern above were widened by mistake.
FORBIDDEN_PREFIXES = (
    "context/ownership/",
    "context/policies/",
    "context/assets/raw/",
    "evals/",
    "src/",
    "tests/",
    ".github/",
    "config/",
    "deploy/",
    "data/",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "docker-compose",
    "Makefile",
)
ELEVATED_FILES = ("context/metrics/*.yaml",)  # any change here needs an elevated reviewer


class PatchError(ValueError):
    pass


def check_repo_path(path: str) -> str:
    """Normalise and authorise one repository-relative path. Raises PatchError if forbidden."""
    raw = path.replace("\\", "/")
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or ".." in p.parts or ":" in raw or raw.startswith("./"):
        raise PatchError(f"path not allowed: {path!r}")
    s = str(p)
    if any(s == f.rstrip("/") or s.startswith(f) for f in FORBIDDEN_PREFIXES):
        raise PatchError(f"path is protected and cannot be changed by a proposal: {s}")
    if not any(fnmatch.fnmatchcase(s, pat) for pat in ALLOWED_PATTERNS):
        raise PatchError(f"path is not in the proposal allow-list: {s}")
    return s


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def serving_schema(serving_path: Path) -> dict[str, set[str]]:
    """table name -> column names of the real serving warehouse (metadata only, read-only)."""
    con = duckdb.connect(str(serving_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'analytics'"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, set[str]] = {}
    for t, c in rows:
        out.setdefault(str(t), set()).add(str(c))
    return out


@dataclass
class StagedResult:
    staged_dir: Path
    changed_files: list[str]
    diffs: list[FileDiff]
    affected_ids: list[str]
    domains: list[str]
    review_tier: str
    proposed_version: str
    validation_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _Repo:
    """Round-trip view of the staged context directory. Indexes object id -> (file, item map)."""

    def __init__(self, repo_root: Path, context_dir: Path) -> None:
        self.repo_root = repo_root
        self.context_dir = context_dir
        self.y = _yaml()
        self.docs: dict[str, Any] = {}
        self.index: dict[str, tuple[str, CommentedMap]] = {}
        for f in sorted(context_dir.rglob("*.yaml")):
            rel = "context/" + f.relative_to(context_dir).as_posix()
            doc = self.y.load(f.read_text(encoding="utf-8"))
            self.docs[rel] = doc
            items = doc.get("items") if isinstance(doc, CommentedMap) else None
            for item in items or []:
                if isinstance(item, CommentedMap) and "id" in item:
                    self.index[str(item["id"])] = (rel, item)
        self.touched: set[str] = set()

    def get(self, oid: str) -> tuple[str, CommentedMap]:
        if oid not in self.index:
            raise PatchError(f"unknown context object {oid}")
        rel, item = self.index[oid]
        check_repo_path(rel)
        self.touched.add(rel)
        return rel, item

    def append(self, rel: str, item: CommentedMap) -> None:
        check_repo_path(rel)
        if rel not in self.docs:
            raise PatchError(f"target file does not exist: {rel}")
        if str(item["id"]) in self.index:
            raise PatchError(f"object {item['id']} already exists")
        self.docs[rel]["items"].append(item)
        self.index[str(item["id"])] = (rel, item)
        self.touched.add(rel)

    def write(self) -> None:
        for rel in self.touched:
            buf = io.StringIO()
            self.y.dump(self.docs[rel], buf)
            (self.repo_root / rel).write_text(buf.getvalue(), encoding="utf-8")


def _flow(seq: list[str]) -> CommentedSeq:
    s = CommentedSeq(seq)
    s.fa.set_flow_style()
    return s


def _draft_meta(author: str, evidence: list[str]) -> tuple[CommentedMap, CommentedMap]:
    cert = CommentedMap(
        {"status": "draft", "notes": "AI-proposed via ContextForge proposal; awaiting human review."}
    )
    cert.fa.set_flow_style()
    prov = CommentedMap(
        {
            "authored_by": author,
            "authored_at": dt.date.today().isoformat(),
            "generated_by": "ai",
            "evidence": _flow(evidence[:10]),
        }
    )
    prov.fa.set_flow_style()
    return cert, prov


def _mark_modified(item: CommentedMap, author: str, evidence: list[str]) -> None:
    """Editing an object makes it a DRAFT again: verified status never survives an AI edit."""
    cert = item.get("certification")
    status = str(cert.get("status")) if isinstance(cert, dict) else "draft"
    if status == "verified":
        new_cert, _ = _draft_meta(author, evidence)
        new_cert["notes"] = (
            "Was verified; AI-proposed edit returns it to draft until re-certified by a human."
        )
        item["certification"] = new_cert
    prov = item.get("provenance")
    if isinstance(prov, dict):
        prov["generated_by"] = "human_and_ai" if str(prov.get("generated_by")) == "human" else "ai"
        ev = prov.get("evidence")
        merged = list(dict.fromkeys([*(list(ev) if ev else []), *evidence]))[:20]
        prov["evidence"] = _flow(merged)


def _domain_of(store: ContextStore, oid: str) -> str | None:
    for coll in (store.assets, store.terms, store.metrics):
        if oid in coll:
            dom = getattr(coll[oid], "domain", None)
            return str(dom) if dom else None
    if oid in store.joins:
        a = store.assets.get(store.joins[oid].left)
        return a.domain if a else None
    return None


def stage(
    *,
    repo_root: Path,
    context_dir: Path,
    serving_path: Path,
    ops: list[PatchOp],
    author: str,
    evidence_refs: list[str],
    work_dir: Path,
) -> StagedResult:
    """Copy the context repo to ``work_dir/context``, apply ``ops``, validate, diff."""
    if not ops:
        raise PatchError("no operations")
    store = load_context(context_dir)
    schema = serving_schema(serving_path)
    staged_root = work_dir
    if staged_root.exists():
        shutil.rmtree(staged_root)
    staged_ctx = staged_root / "context"
    shutil.copytree(context_dir, staged_ctx)
    repo = _Repo(staged_root, staged_ctx)
    affected: list[str] = []
    tier = "standard"
    owners = {o.id for o in store.owners.values() if o.kind in ("team", "human")}

    def need_owner(oid: str) -> None:
        if oid not in owners:
            raise PatchError(f"owner {oid} must be an existing team or human in owners.yaml")

    def need_asset(aid: str) -> Any:
        a = store.assets.get(aid)
        if a is None or a.layer != "serving":
            raise PatchError(f"{aid} is not a governed serving asset")
        if a.table not in schema:
            raise PatchError(f"{aid}: table analytics.{a.table} not found in the warehouse schema")
        return a

    for op in ops:
        if op.op in ELEVATED_OPS:
            tier = "elevated"
        if isinstance(op, SetColumnDescription):
            a = need_asset(op.asset_id)
            if op.column not in schema[a.table]:
                raise PatchError(
                    f"column {op.column} does not exist in analytics.{a.table} (schema evidence)"
                )
            rel, item = repo.get(op.asset_id)
            cols = item["columns"]
            match = next((c for c in cols if str(c.get("name")) == op.column), None)
            if match is None:
                raise PatchError(
                    f"column {op.column} exists in the warehouse but is not documented on {op.asset_id}; "
                    "adding a column (type + sensitivity classification) needs a human steward"
                )
            match["description"] = op.description
            _mark_modified(item, author, evidence_refs)
            affected.append(op.asset_id)
        elif isinstance(op, AddTermSynonym):
            rel, item = repo.get(op.target_id)
            if not op.target_id.startswith(("term.", "metric.", "unsupported.")):
                raise PatchError(
                    "synonyms can only be added to glossary terms, metrics or unsupported concepts"
                )
            syn = item.get("synonyms")
            if syn is None:
                item["synonyms"] = _flow([op.synonym])
            elif op.synonym.lower() not in {str(s).lower() for s in syn}:
                syn.append(op.synonym)
            if op.target_id.startswith(("term.", "metric.")):
                _mark_modified(item, author, evidence_refs)
            if op.target_id.startswith("metric."):
                tier = "elevated"
            affected.append(op.target_id)
        elif isinstance(op, AppendTermNote):
            if not op.term_id.startswith("term."):
                raise PatchError("append_term_note targets glossary terms")
            rel, item = repo.get(op.term_id)
            seq = item.get(op.field)
            if seq is None:
                item[op.field] = CommentedSeq([op.text])
            else:
                seq.append(op.text)
            _mark_modified(item, author, evidence_refs)
            affected.append(op.term_id)
        elif isinstance(op, AddGlossaryTerm):
            need_owner(op.owner)
            for aid in op.assets:
                need_asset(aid)
            for col in op.columns:
                aid, _, cname = col.partition(":")
                a = need_asset(aid)
                if cname not in schema[a.table]:
                    raise PatchError(f"column {col} not in warehouse schema")
            for mid in op.metrics:
                if mid not in store.metrics:
                    raise PatchError(f"unknown metric {mid}")
            cert, prov = _draft_meta(author, [*op.evidence, *evidence_refs])
            item = CommentedMap({"id": op.id, "name": op.name})
            item["synonyms"] = _flow(op.synonyms)
            item["definition"] = op.definition
            if op.business_interpretation:
                item["business_interpretation"] = op.business_interpretation
            item["domain"] = op.domain
            item["owner"] = op.owner
            for key in ("assets", "columns", "metrics", "ambiguities"):
                vals = getattr(op, key)
                if vals:
                    item[key] = _flow(vals)
            item["certification"] = cert
            item["provenance"] = prov
            if not op.file.startswith("context/glossary/"):
                raise PatchError("glossary terms must be added to a context/glossary/ file")
            repo.append(check_repo_path(op.file), item)
            affected.append(op.id)
        elif isinstance(op, AddJoin):
            need_owner(op.owner)
            la, ra = need_asset(op.left), need_asset(op.right)
            for pair in op.join_columns:
                if set(pair) != {"left", "right"}:
                    raise PatchError("join_columns entries need exactly left/right")
                if pair["left"] not in schema[la.table] or pair["right"] not in schema[ra.table]:
                    raise PatchError(f"join column pair {pair} not in warehouse schema")
            cert, prov = _draft_meta(author, evidence_refs)
            item = CommentedMap({"id": op.id, "left": op.left, "right": op.right})
            jc = CommentedSeq()
            for pair in op.join_columns:
                m = CommentedMap(pair)
                m.fa.set_flow_style()
                jc.append(m)
            item["join_columns"] = jc
            item["cardinality"] = op.cardinality
            item["grain_note"] = op.grain_note
            if op.fanout_warning:
                item["fanout_warning"] = op.fanout_warning
            item["observed"] = op.observed
            item["owner"] = op.owner
            item["certification"] = cert
            item["provenance"] = prov
            repo.append("context/joins/joins.yaml", item)
            affected.append(op.id)
        elif isinstance(op, SetFanoutWarning):
            if op.join_id not in store.joins:
                raise PatchError(f"unknown join {op.join_id}")
            rel, item = repo.get(op.join_id)
            item["fanout_warning"] = op.fanout_warning
            _mark_modified(item, author, evidence_refs)
            affected.append(op.join_id)
        elif isinstance(op, AddMetricCaveat):
            if op.metric_id not in store.metrics:
                raise PatchError(f"unknown metric {op.metric_id}")
            rel, item = repo.get(op.metric_id)
            seq = item.get(op.field)
            if seq is None:
                item[op.field] = CommentedSeq([op.text])
            else:
                seq.append(op.text)
            _mark_modified(item, author, evidence_refs)
            affected.append(op.metric_id)
        elif isinstance(op, ChangeMetricSql):
            if op.metric_id not in store.metrics:
                raise PatchError(f"unknown metric {op.metric_id}")
            rel, item = repo.get(op.metric_id)
            item["sql"] = op.sql
            item["sql_status"] = "draft"
            item["double_counting_check"] = op.double_counting_check
            item["version"] = int(item.get("version", 1)) + 1
            _mark_modified(item, author, evidence_refs)
            affected.append(op.metric_id)
        elif isinstance(op, DeprecateObject):
            if op.target_id not in store.assets and op.target_id not in store.metrics:
                raise PatchError("only assets and metrics can be deprecated")
            if op.replaced_by and store.kind_of(op.replaced_by) is None:
                raise PatchError(f"replacement {op.replaced_by} does not exist")
            rel, item = repo.get(op.target_id)
            cert = CommentedMap({"status": "deprecated", "deprecated_reason": op.reason})
            if op.replaced_by:
                cert["replaced_by"] = op.replaced_by
            cert.fa.set_flow_style()
            item["certification"] = cert
            _mark_modified(item, author, evidence_refs)
            affected.append(op.target_id)
        elif isinstance(op, AddUnsupportedConcept):
            need_owner(op.decision_needed_from)
            for mid in op.candidate_definitions:
                if mid not in store.metrics:
                    raise PatchError(f"unknown candidate metric {mid}")
            reason = op.reason
            if op.source_data_required:
                reason += f" Source data required to support it: {op.source_data_required}"
            item = CommentedMap(
                {
                    "id": op.id,
                    "name": op.name,
                    "synonyms": _flow(op.synonyms),
                    "classification": op.classification,
                    "reason": reason,
                }
            )
            if op.candidate_definitions:
                item["candidate_definitions"] = _flow(op.candidate_definitions)
            item["decision_needed_from"] = op.decision_needed_from
            item["guidance"] = op.guidance
            repo.append("context/unsupported/concepts.yaml", item)
            affected.append(op.id)
        else:  # pragma: no cover - discriminated union is exhaustive
            raise PatchError(f"unsupported op {op}")

    for rel in repo.touched:
        check_repo_path(rel)
        if any(fnmatch.fnmatchcase(rel, p) for p in ELEVATED_FILES) and any(
            isinstance(o, ChangeMetricSql | DeprecateObject) for o in ops
        ):
            tier = "elevated"
    repo.write()

    _, issues = validate_directory(staged_ctx)
    errs = [str(e) for e in errors(issues)]
    new_store = load_context(staged_ctx) if not errs else None
    diffs: list[FileDiff] = []
    for rel in sorted(repo.touched):
        before = (repo_root / rel).read_text(encoding="utf-8") if (repo_root / rel).is_file() else ""
        after = (staged_root / rel).read_text(encoding="utf-8")
        if before == after:
            continue
        diffs.append(
            FileDiff(
                path=rel,
                before_sha256=hashlib.sha256(before.encode()).hexdigest() if before else None,
                after_sha256=hashlib.sha256(after.encode()).hexdigest(),
                unified_diff="".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                        n=2,
                    )
                ),
            )
        )
    domains = sorted({d for d in (_domain_of(store, i) for i in affected) if d})
    return StagedResult(
        staged_dir=staged_ctx,
        changed_files=[d.path for d in diffs],
        diffs=diffs,
        affected_ids=list(dict.fromkeys(affected)),
        domains=domains,
        review_tier=tier,
        proposed_version=new_store.version if new_store else "invalid",
        validation_errors=errs,
    )
