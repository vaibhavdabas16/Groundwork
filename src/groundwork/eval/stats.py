"""Aggregate metrics and paired statistics.

Unit of analysis: the QUESTION. Repetitions of the same question are not independent samples;
per-question scores are averaged over repetitions first, and bootstrap resampling is over
questions. Significance (exact McNemar on per-question majority-vote correctness) is reported
only for the pre-registered comparisons in docs/EVALUATION_METHODOLOGY.md, with Holm correction.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from typing import Any

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261120
MODE_ORDER = ("raw_schema", "descriptions", "full_context")
PREREGISTERED = (
    ("raw_schema", "full_context"),
    ("raw_schema", "descriptions"),
    ("descriptions", "full_context"),
)


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def mode_scorecard(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    sc = [a["score"] for a in attempts]
    ans = [s for s in sc if s["answerable"]]
    safe = [s for s in sc if not s["answerable"]]
    answered = [s for s in ans if s["outcome"] == "ANSWER"]
    rq_calls = sum(s["run_query_calls"] for s in sc)
    tool_calls = sum(s["tool_calls"] for s in sc)
    lat = [s["latency_ms"] for s in sc]
    costs = [s["cost_usd"] for s in sc if s["cost_usd"] is not None]
    retr = [s["retrieval_success"] for s in sc if s["retrieval_success"] is not None]
    per_case: dict[str, list[bool]] = defaultdict(list)
    per_case_outcome: dict[str, list[str]] = defaultdict(list)
    for s in sc:
        per_case[s["case_id"]].append(bool(s["correct"]))
        per_case_outcome[s["case_id"]].append(s["outcome"])
    multi = {k: v for k, v in per_case.items() if len(v) > 1}
    correct_answers = sum(s["correct"] for s in ans)
    return {
        "attempts": len(sc),
        "cases": len(per_case),
        "answerable_attempts": len(ans),
        "safe_attempts": len(safe),
        "A_answer_accuracy": _rate(correct_answers, len(ans)),
        "A_answer_accuracy_majority_vote": _rate(
            sum(sum(per_case[k]) * 2 > len(per_case[k]) for k in {s["case_id"] for s in ans}),
            len({s["case_id"] for s in ans}),
        ),
        "B_safe_outcome_rate": _rate(sum(s["correct"] for s in safe), len(safe)),
        "C_false_confident_rate_safe_cases": _rate(sum(s["false_confident"] for s in safe), len(safe)),
        "C2_wrong_answer_share_of_answers": _rate(sum(not s["correct"] for s in answered), len(answered)),
        "D_unnecessary_abstention_rate": _rate(sum(s["unnecessary_abstention"] for s in ans), len(ans)),
        "execution_error_rate": _rate(sum(s["execution_error"] for s in sc), len(sc)),
        "E_sql_execution_success_rate": _rate(sum(s["run_query_ok"] for s in sc), rq_calls),
        "E2_attempts_with_successful_query": _rate(sum(s["run_query_ok"] > 0 for s in ans), len(ans)),
        "F_consistency_same_correctness": _rate(sum(len(set(v)) == 1 for v in multi.values()), len(multi)),
        "F2_consistency_same_outcome": _rate(
            sum(len(set(per_case_outcome[k])) == 1 for k in multi), len(multi)
        ),
        "G_latency_ms_median": _pct(lat, 0.5),
        "G_latency_ms_p95": _pct(lat, 0.95),
        "H_input_tokens_total": sum(s["input_tokens"] for s in sc),
        "H_output_tokens_total": sum(s["output_tokens"] for s in sc),
        "H_cache_read_tokens_total": sum(s["cache_read_tokens"] for s in sc),
        "H_cache_write_tokens_total": sum(s["cache_write_tokens"] for s in sc),
        "H_tokens_per_attempt_mean": (
            statistics.fmean(
                s["input_tokens"] + s["output_tokens"] + s["cache_read_tokens"] + s["cache_write_tokens"]
                for s in sc
            )
            if sc
            else None
        ),
        "I_cost_usd_total": round(sum(costs), 4) if costs else None,
        "I_cost_usd_per_attempt": round(statistics.fmean(costs), 5) if costs else None,
        "I_cost_usd_per_correct_answer": round(sum(costs) / correct_answers, 5)
        if costs and correct_answers
        else None,
        "I_cost_unknown_attempts": sum(s["cost_usd"] is None for s in sc),
        "J_context_retrieval_success": _rate(sum(retr), len(retr)),
        "K_invalid_tool_call_rate": _rate(sum(s["invalid_tool_calls"] for s in sc), tool_calls),
        "L_privacy_violations": sum(len(s["privacy_violations"]) for s in sc),
        "L_security_violations": sum(len(s["security_violations"]) for s in sc),
        "N_tool_calls_mean": statistics.fmean(s["tool_calls"] for s in sc) if sc else None,
        "O_sql_correction_attempts_mean": statistics.fmean(s["sql_repairs"] for s in sc) if sc else None,
        "labels": dict(Counter(s["label"] for s in sc)),
        "error_codes": dict(Counter(s["error_code"] for s in sc if s["error_code"])),
        "resolved_models": sorted({m for s in sc for m in s["resolved_models"]}),
    }


def per_category(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in attempts:
        groups[a["score"]["category"]].append(a["score"])
    for cat, sc in sorted(groups.items()):
        out[cat] = {
            "attempts": len(sc),
            "success_rate": _rate(sum(s["correct"] for s in sc), len(sc)),
            "failure_rate": _rate(sum(not s["correct"] for s in sc), len(sc)),
            "labels": dict(Counter(s["label"] for s in sc)),
        }
    return out


def repetition_variability(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    by_rep: dict[int, list[bool]] = defaultdict(list)
    for a in attempts:
        if a["score"]["answerable"]:
            by_rep[a["repetition"]].append(bool(a["score"]["correct"]))
    accs = {rep: sum(v) / len(v) for rep, v in sorted(by_rep.items()) if v}
    vals = list(accs.values())
    return {
        "accuracy_by_repetition": accs,
        "stdev_across_repetitions": statistics.pstdev(vals) if len(vals) > 1 else None,
        "range_across_repetitions": (max(vals) - min(vals)) if vals else None,
    }


def _case_means(attempts: list[dict[str, Any]], answerable: bool) -> dict[str, float]:
    d: dict[str, list[float]] = defaultdict(list)
    for a in attempts:
        if a["score"]["answerable"] == answerable:
            d[a["case_id"]].append(1.0 if a["score"]["correct"] else 0.0)
    return {k: statistics.fmean(v) for k, v in d.items()}


def _binom_two_sided(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def paired_comparison(
    a_attempts: list[dict[str, Any]], b_attempts: list[dict[str, Any]], *, answerable: bool = True
) -> dict[str, Any]:
    """Paired comparison B - A over questions present in both."""
    ma, mb = _case_means(a_attempts, answerable), _case_means(b_attempts, answerable)
    keys = sorted(set(ma) & set(mb))
    if not keys:
        return {"n_questions": 0}
    diffs = [mb[k] - ma[k] for k in keys]
    mean_a = statistics.fmean(ma[k] for k in keys)
    mean_b = statistics.fmean(mb[k] for k in keys)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - reproducible statistics, not crypto
    boots = []
    n = len(keys)
    for _ in range(BOOTSTRAP_RESAMPLES):
        boots.append(statistics.fmean(diffs[rng.randrange(n)] for _ in range(n)))
    lo, hi = _pct(boots, 0.025), _pct(boots, 0.975)
    maj_a = {k: ma[k] > 0.5 for k in keys}
    maj_b = {k: mb[k] > 0.5 for k in keys}
    b_only = sum(maj_b[k] and not maj_a[k] for k in keys)
    a_only = sum(maj_a[k] and not maj_b[k] for k in keys)
    return {
        "n_questions": n,
        "score_a": mean_a,
        "score_b": mean_b,
        "absolute_pp": 100 * (mean_b - mean_a),
        "relative_pct": (100 * (mean_b - mean_a) / mean_a) if mean_a > 0 else None,
        "bootstrap_ci95_pp": [100 * lo, 100 * hi] if lo is not None and hi is not None else None,
        "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "unit": "question", "seed": BOOTSTRAP_SEED},
        "questions_improved": sum(d > 0 for d in diffs),
        "questions_worse": sum(d < 0 for d in diffs),
        "questions_unchanged": sum(d == 0 for d in diffs),
        "mcnemar_exact": {
            "b_correct_a_wrong": b_only,
            "a_correct_b_wrong": a_only,
            "p_value": _binom_two_sided(b_only, a_only),
        },
        "per_question_diff": {k: mb[k] - ma[k] for k in keys},
    }


def holm(pvals: dict[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, dict[str, Any]] = {}
    still = True
    for i, (k, p) in enumerate(items):
        thresh = alpha / (m - i)
        rej = still and p <= thresh
        if not rej:
            still = False
        out[k] = {"p_value": p, "holm_threshold": thresh, "reject_h0": rej}
    return out


def summarize(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in attempts:
        by_mode[a["mode"]].append(a)
    modes = [m for m in MODE_ORDER if m in by_mode] + sorted(set(by_mode) - set(MODE_ORDER))
    out: dict[str, Any] = {
        "overall": mode_scorecard(attempts),
        "modes": {m: mode_scorecard(by_mode[m]) for m in modes},
        "per_category": {m: per_category(by_mode[m]) for m in modes},
        "repetition_variability": {m: repetition_variability(by_mode[m]) for m in modes},
        "comparisons": {},
    }
    pv: dict[str, float] = {}
    for ma, mb in PREREGISTERED:
        if ma in by_mode and mb in by_mode:
            key = f"{ma}->{mb}"
            ans = paired_comparison(by_mode[ma], by_mode[mb], answerable=True)
            safe = paired_comparison(by_mode[ma], by_mode[mb], answerable=False)
            cat_diff = {}
            ca, cb = per_category(by_mode[ma]), per_category(by_mode[mb])
            for cat in sorted(set(ca) & set(cb)):
                ra, rb = ca[cat]["success_rate"], cb[cat]["success_rate"]
                cat_diff[cat] = None if ra is None or rb is None else 100 * (rb - ra)
            sa, sb = out["modes"][ma], out["modes"][mb]
            out["comparisons"][key] = {
                "answer_accuracy": ans,
                "safe_outcome_rate": safe,
                "per_category_success_pp": cat_diff,
                "cost_latency": {
                    "cost_per_attempt_usd": [sa["I_cost_usd_per_attempt"], sb["I_cost_usd_per_attempt"]],
                    "latency_median_ms": [sa["G_latency_ms_median"], sb["G_latency_ms_median"]],
                    "tokens_per_attempt": [sa["H_tokens_per_attempt_mean"], sb["H_tokens_per_attempt_mean"]],
                },
            }
            if ans.get("n_questions"):
                pv[key] = ans["mcnemar_exact"]["p_value"]
    out["significance_holm"] = holm(pv) if pv else {}
    return out
