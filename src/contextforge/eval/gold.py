"""Gold-label lifecycle: authored -> sql_validated -> human_reviewed, plus held-out freezing.

* ``freeze``: executes every gold SQL (and documented alternatives) through the governed query
  path (``ContextForgeService.run_query``) on the current snapshot and writes
  ``evals/expected/<case>.json`` and ``evals/manifest.json``. Expected results therefore come from
  reviewed gold SQL on a fixed snapshot - never from agent output.
* ``lock_heldout``: records hashes of held-out cases and expected results in
  ``evals/heldout_lock.json``. Re-locking requires a reason, appended to ``evals/LABEL_CHANGELOG.md``.
* ``record_review``: a human reviewer (``kind: human``, ``placeholder: false`` in
  ``context/ownership/owners.yaml``) approves a case at a specific content hash. A later edit to the
  case or its expected result silently invalidates the review (status falls back to sql_validated).
  AI cross-checks are stored separately and never count as human review.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml

from contextforge.agent.provenance import dataset_snapshot
from contextforge.config.settings import ContextMode, Settings
from contextforge.context.loader import load_context
from contextforge.eval.cases import Comparator, EvalCase, load_cases
from contextforge.eval.compare import compare_one

GOLD_PRINCIPAL = "principal.local_analyst"
ReviewStatus = Literal["authored", "sql_validated", "human_reviewed"]


class GoldError(RuntimeError):
    pass


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class GoldPaths:
    def __init__(self, root: Path) -> None:
        self.root = root / "evals"
        self.cases = self.root / "cases"
        self.expected = self.root / "expected"
        self.manifest = self.root / "manifest.json"
        self.lock = self.root / "heldout_lock.json"
        self.reviews = self.root / "reviews.yaml"
        self.changelog = self.root / "LABEL_CHANGELOG.md"

    def expected_file(self, case_id: str) -> Path:
        return self.expected / f"{case_id}.json"


def load_expected(paths: GoldPaths, case_id: str) -> dict[str, Any] | None:
    f = paths.expected_file(case_id)
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def expected_hash(exp: dict[str, Any]) -> str:
    return _sha({k: exp[k] for k in ("columns", "rows", "alternatives", "case_hash", "snapshot_sha256")})


def freeze(settings: Settings) -> dict[str, Any]:
    from contextforge.service.core import ContextForgeService

    paths = GoldPaths(settings.root)
    cases = load_cases(paths.cases)
    snap = dataset_snapshot(settings.serving_path, settings.manifest_dir)["snapshot_id"]
    svc = ContextForgeService(settings, principal_id=GOLD_PRINCIPAL, context_mode=ContextMode.full_context)
    paths.expected.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, Any]] = []
    entries: dict[str, Any] = {}
    try:
        for case in cases:
            entry: dict[str, Any] = {
                "split": case.split,
                "case_hash": case.content_hash(),
                "answerable": case.answerable,
            }
            gold_sql = case.gold_sql or (case.answer_check.gold_sql if case.answer_check else None)
            if gold_sql is not None:
                results = []
                for label, sql in [("primary", gold_sql)] + [
                    (f"alternative_{i + 1}", a.gold_sql) for i, a in enumerate(case.alternatives)
                ]:
                    r = svc.run_query(sql, {"request_id": f"gold-{case.id}"[:64], "purpose": "gold freeze"})
                    if not r.ok or r.truncated or r.row_count == 0:
                        failures.append(
                            {
                                "case_id": case.id,
                                "which": label,
                                "error": r.error.model_dump() if r.error else None,
                                "truncated": r.truncated,
                                "row_count": r.row_count,
                            }
                        )
                        break
                    results.append(
                        {
                            "label": label,
                            "sql": sql,
                            "columns": r.columns,
                            "column_types": r.column_types,
                            "rows": r.rows,
                        }
                    )
                else:
                    primary = results[0]
                    exp = {
                        "case_id": case.id,
                        "case_hash": case.content_hash(),
                        "snapshot_sha256": snap,
                        "context_version": svc.store.version,
                        "executed_via": "ContextForgeService.run_query (governed path, principal.local_analyst)",
                        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                        "sql_sha256": hashlib.sha256(gold_sql.encode()).hexdigest(),
                        "columns": primary["columns"],
                        "column_types": primary["column_types"],
                        "rows": primary["rows"],
                        "row_count": len(primary["rows"]),
                        "alternatives": [
                            {"label": a["label"], "columns": a["columns"], "rows": a["rows"]}
                            for a in results[1:]
                        ],
                    }
                    paths.expected_file(case.id).write_text(
                        json.dumps(exp, indent=1, default=str) + "\n", encoding="utf-8"
                    )
                    entry["expected_sha256"] = expected_hash(exp)
            entries[case.id] = entry
    finally:
        svc.close()
    manifest = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "snapshot_sha256": snap,
        "counts": {
            "total": len(cases),
            "answerable": sum(c.answerable for c in cases),
            "safe_outcome": sum(not c.answerable for c in cases),
            "dev": sum(c.split == "dev" for c in cases),
            "heldout": sum(c.split == "heldout" for c in cases),
        },
        "failures": failures,
        "cases": entries,
    }
    paths.manifest.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def _load_reviews(paths: GoldPaths) -> dict[str, Any]:
    if not paths.reviews.is_file():
        return {"reviews": [], "ai_cross_checks": []}
    doc = yaml.safe_load(paths.reviews.read_text(encoding="utf-8")) or {}
    doc.setdefault("reviews", [])
    doc.setdefault("ai_cross_checks", [])
    return doc


def case_status(
    paths: GoldPaths, case: EvalCase, snapshot_sha: str, reviews: dict[str, Any] | None = None
) -> ReviewStatus:
    reviews = reviews if reviews is not None else _load_reviews(paths)
    if has_gold_sql(case):
        exp = load_expected(paths, case.id)
        if (
            exp is None
            or exp.get("case_hash") != case.content_hash()
            or exp.get("snapshot_sha256") != snapshot_sha
        ):
            return "authored"
        ehash = expected_hash(exp)
    else:
        ehash = None  # outcome-only safe labels have no SQL to validate
    for r in reviews["reviews"]:
        if (
            r.get("case_id") == case.id
            and r.get("case_hash") == case.content_hash()
            and r.get("expected_sha256") == ehash
            and r.get("status") == "human_reviewed"
        ):
            return "human_reviewed"
    return "sql_validated" if has_gold_sql(case) else "authored"


def has_gold_sql(case: EvalCase) -> bool:
    return case.gold_sql is not None or bool(case.answer_check and case.answer_check.gold_sql)


def status_report(settings: Settings) -> dict[str, Any]:
    paths = GoldPaths(settings.root)
    cases = load_cases(paths.cases)
    snap = dataset_snapshot(settings.serving_path, settings.manifest_dir)["snapshot_id"]
    reviews = _load_reviews(paths)
    rows = []
    for c in cases:
        rows.append(
            {
                "case_id": c.id,
                "split": c.split,
                "answerable": c.answerable,
                "status": case_status(paths, c, snap, reviews),
            }
        )
    lock = verify_lock(settings)
    summary: dict[str, Any] = {}
    for r in rows:
        key = f"{r['split']}.{'answerable' if r['answerable'] else 'safe'}.{r['status']}"
        summary[key] = summary.get(key, 0) + 1
    return {
        "snapshot_sha256": snap,
        "summary": dict(sorted(summary.items())),
        "heldout_lock": lock,
        "ai_cross_checks": len(reviews["ai_cross_checks"]),
        "cases": rows,
    }


def _heldout_fingerprint(paths: GoldPaths, cases: list[EvalCase]) -> dict[str, Any]:
    out = {}
    for c in cases:
        if c.split != "heldout":
            continue
        exp = load_expected(paths, c.id) if has_gold_sql(c) else None
        out[c.id] = {"case_hash": c.content_hash(), "expected_sha256": expected_hash(exp) if exp else None}
    return out


def lock_heldout(settings: Settings, *, reason: str | None = None) -> dict[str, Any]:
    paths = GoldPaths(settings.root)
    cases = load_cases(paths.cases)
    if paths.lock.is_file() and not reason:
        raise GoldError(
            "held-out set is already locked; re-locking requires --reason (logged to LABEL_CHANGELOG.md)"
        )
    fp = _heldout_fingerprint(paths, cases)
    missing = [
        cid
        for cid, v in fp.items()
        if v["expected_sha256"] is None and has_gold_sql(next(c for c in cases if c.id == cid))
    ]
    if missing:
        raise GoldError(f"cannot lock: answerable held-out cases without expected results: {missing}")
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    previous = json.loads(paths.lock.read_text()) if paths.lock.is_file() else None
    lock = {
        "locked_at": now,
        "snapshot_sha256": dataset_snapshot(settings.serving_path, settings.manifest_dir)["snapshot_id"],
        "case_count": len(fp),
        "fingerprint_sha256": _sha(fp),
        "cases": fp,
    }
    paths.lock.write_text(json.dumps(lock, indent=1) + "\n", encoding="utf-8")
    changed = []
    if previous:
        changed = sorted(
            set(fp) ^ set(previous["cases"])
            | {k for k in fp if k in previous["cases"] and previous["cases"][k] != fp[k]}
        )
    with open(paths.changelog, "a", encoding="utf-8") as f:
        f.write(
            f"\n## {now} - held-out {'re-' if previous else ''}lock\n"
            f"- fingerprint: {lock['fingerprint_sha256'][:16]} ({len(fp)} cases)\n"
            f"- reason: {reason or 'initial freeze before any experimental comparison'}\n"
            + (f"- changed cases: {', '.join(changed) or 'none'}\n" if previous else "")
        )
    return lock


def verify_lock(settings: Settings) -> dict[str, Any]:
    paths = GoldPaths(settings.root)
    if not paths.lock.is_file():
        return {"locked": False, "matches": False, "changed": []}
    lock = json.loads(paths.lock.read_text())
    fp = _heldout_fingerprint(paths, load_cases(paths.cases))
    changed = sorted(
        set(fp) ^ set(lock["cases"]) | {k for k in fp if k in lock["cases"] and lock["cases"][k] != fp[k]}
    )
    return {"locked": True, "locked_at": lock["locked_at"], "matches": not changed, "changed": changed}


def check(settings: Settings) -> dict[str, Any]:
    """Re-execute every gold query and compare to the committed expected results (no writes)."""
    from contextforge.service.core import ContextForgeService

    paths = GoldPaths(settings.root)
    snap = dataset_snapshot(settings.serving_path, settings.manifest_dir)["snapshot_id"]
    svc = ContextForgeService(settings, principal_id=GOLD_PRINCIPAL, context_mode=ContextMode.full_context)
    problems: list[dict[str, Any]] = []
    checked = 0
    try:
        for case in load_cases(paths.cases):
            gold_sql = case.gold_sql or (case.answer_check.gold_sql if case.answer_check else None)
            if gold_sql is None:
                continue
            exp = load_expected(paths, case.id)
            if exp is None or exp.get("case_hash") != case.content_hash():
                problems.append({"case_id": case.id, "problem": "expected result missing or stale"})
                continue
            if exp.get("snapshot_sha256") != snap:
                problems.append(
                    {
                        "case_id": case.id,
                        "problem": f"snapshot differs ({exp.get('snapshot_sha256')} != {snap})",
                    }
                )
            sqls = [gold_sql] + [a.gold_sql for a in case.alternatives]
            wanted = [exp["rows"]] + [a["rows"] for a in exp.get("alternatives", [])]
            for sql, rows in zip(sqls, wanted, strict=True):
                r = svc.run_query(sql, {"request_id": f"gold-check-{case.id}"[:64]})
                checked += 1
                strict = Comparator(
                    type="rows",
                    ordered=bool(case.comparator and case.comparator.ordered),
                    abs_tolerance=1e-9,
                    rel_tolerance=1e-9,
                )
                same = r.ok and compare_one(exp["columns"], rows, r.columns, r.rows, strict).match
                if not same:
                    problems.append(
                        {
                            "case_id": case.id,
                            "problem": "result differs from expected",
                            "error": r.error.model_dump() if r.error else None,
                        }
                    )
    finally:
        svc.close()
    return {"snapshot_id": snap, "queries_checked": checked, "problems": problems, "ok": not problems}


def record_review(
    settings: Settings,
    case_id: str,
    *,
    reviewer: str,
    decision_ref: str,
    notes: str | None = None,
) -> dict[str, Any]:
    paths = GoldPaths(settings.root)
    store = load_context(settings.context_dir)
    owner = store.owners.get(reviewer)
    if owner is None or owner.kind != "human" or owner.placeholder:
        raise GoldError(
            f"reviewer {reviewer!r} must be a non-placeholder owner of kind 'human' in context/ownership/owners.yaml"
        )
    case = next((c for c in load_cases(paths.cases) if c.id == case_id), None)
    if case is None:
        raise GoldError(f"unknown case {case_id}")
    ehash = None
    if has_gold_sql(case):
        exp = load_expected(paths, case_id)
        if exp is None or exp.get("case_hash") != case.content_hash():
            raise GoldError(
                f"{case_id}: freeze gold results before review (run `contextforge eval gold freeze`)"
            )
        ehash = expected_hash(exp)
    doc = _load_reviews(paths)
    entry = {
        "case_id": case_id,
        "status": "human_reviewed",
        "reviewer": reviewer,
        "reviewed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "case_hash": case.content_hash(),
        "expected_sha256": ehash,
        "decision_ref": decision_ref,
        "notes": notes,
    }
    doc["reviews"].append(entry)
    paths.reviews.write_text(
        _REVIEWS_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return entry


_REVIEWS_HEADER = """\
# Gold-label review ledger. Written by `contextforge eval gold review` (human reviews) and by
# documented AI cross-checks. ONLY entries with status human_reviewed whose case_hash and
# expected_sha256 match the current case count as human review. AI cross-checks never do.
"""
