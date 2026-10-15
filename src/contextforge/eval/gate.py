"""Regression gate over a finished evaluation run. Policy: evals/regression_policy.yaml.

Exit semantics (also used by CI):
  PASS     (0) every requirement met
  FAIL     (1) a requirement was evaluated and violated
  BLOCKED  (3) the gate could not be evaluated: no live run, run INCOMPLETE/FAILED, gold not
               human-reviewed, held-out lock mismatch, missing credentials... BLOCKED is never green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from contextforge.eval.stats import paired_comparison, summarize
from contextforge.eval.store import EvalStore

EXIT = {"PASS": 0, "FAIL": 1, "BLOCKED": 3}


def load_policy(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc


def evaluate_gate(
    store: EvalStore, run_id: str | None, policy: dict[str, Any], *, baseline_run_id: str | None = None
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocked: list[str] = []
    failed: list[str] = []

    def check(name: str, ok: bool | None, detail: str, *, blocking: bool = False) -> None:
        state = "pass" if ok else ("blocked" if (ok is None or blocking) else "fail")
        checks.append({"check": name, "result": state, "detail": detail})
        if state == "blocked":
            blocked.append(name)
        elif state == "fail":
            failed.append(name)

    if run_id is None:
        check("live_run_exists", None, "no evaluation run supplied (live evaluation NOT_RUN)")
        return _verdict(checks, blocked, failed)
    run = store.run(run_id)
    if run is None:
        check("live_run_exists", None, f"run {run_id} not found")
        return _verdict(checks, blocked, failed)
    req = policy["required"]
    check(
        "run_status_complete",
        run["status"] == "COMPLETE" or None,
        f"status={run['status']} {run.get('status_reason') or ''}",
    )
    check("provider_live", bool(run["provider_live"]) or None, f"provider={run['provider']}")
    lock = (run.get("gold") or {}).get("heldout_lock") or {}
    check("heldout_locked_and_unchanged", (lock.get("locked") and lock.get("matches")) or None, str(lock))
    attempts = store.attempts(run_id)
    statuses = (run.get("gold") or {}).get("case_statuses", {})
    reviewed = {c for c, st in statuses.items() if st == "human_reviewed"}
    gated = [
        a
        for a in attempts
        if a["split"] == "heldout"
        and (a["case_id"] in reviewed or not req.get("require_human_reviewed_gold", True))
    ]
    n_cases = len({a["case_id"] for a in gated})
    check(
        "enough_reviewed_heldout_cases",
        n_cases >= req["min_gated_heldout_cases"] or None,
        f"{n_cases} gated held-out cases (human-reviewed={len(reviewed)}); need {req['min_gated_heldout_cases']}",
    )
    sec = policy["security"]
    all_summary = summarize(attempts)["overall"]
    check(
        "privacy_violations",
        all_summary["L_privacy_violations"] <= sec["max_privacy_violations"],
        f"{all_summary['L_privacy_violations']} found",
    )
    check(
        "security_violations",
        all_summary["L_security_violations"] <= sec["max_security_violations"],
        f"{all_summary['L_security_violations']} found",
    )
    q = policy["quality"]
    mode_attempts = [a for a in gated if a["mode"] == q["mode"]]
    if not mode_attempts:
        check("quality_mode_present", None, f"no gated attempts in mode {q['mode']}")
        return _verdict(checks, blocked, failed)
    s = summarize(mode_attempts)["overall"]
    for key, field, op in (
        ("min_answer_accuracy", "A_answer_accuracy", ">="),
        ("min_safe_outcome_rate", "B_safe_outcome_rate", ">="),
        ("max_false_confident_rate", "C_false_confident_rate_safe_cases", "<="),
    ):
        val = s[field]
        if val is None:
            check(key, None, f"{field} not computable")
            continue
        ok = val >= q[key] if op == ">=" else val <= q[key]
        check(key, ok, f"{field}={val:.3f} {op} {q[key]}")
    reg = policy.get("regression") or {}
    if baseline_run_id:
        base = store.attempts(baseline_run_id)
        b_mode = [a for a in base if a["mode"] == q["mode"] and a["split"] == "heldout"]
        cmp = paired_comparison(b_mode, mode_attempts, answerable=True)
        if not cmp.get("n_questions"):
            check("regression_vs_baseline", None, "no overlapping questions with baseline")
        else:
            drop = -cmp["absolute_pp"]
            ci_hi = cmp["bootstrap_ci95_pp"][1]
            regressed = drop > reg["max_accuracy_drop_pp"] and ci_hi < 0
            check(
                "regression_vs_baseline",
                not regressed,
                f"diff={cmp['absolute_pp']:+.1f}pp CI95={cmp['bootstrap_ci95_pp']} (fail only if drop > "
                f"{reg['max_accuracy_drop_pp']}pp AND CI upper bound < 0)",
            )
    return _verdict(checks, blocked, failed)


def _verdict(checks: list[dict[str, Any]], blocked: list[str], failed: list[str]) -> dict[str, Any]:
    verdict = "FAIL" if failed else ("BLOCKED" if blocked else "PASS")
    return {
        "verdict": verdict,
        "exit_code": EXIT[verdict],
        "failed": failed,
        "blocked": blocked,
        "checks": checks,
    }
