"""Run artifacts: summary.json, report.md, attempts.csv, plus run-vs-run comparison."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from groundwork.eval.stats import MODE_ORDER, paired_comparison, summarize
from groundwork.eval.store import EvalStore

METRIC_ROWS = [
    ("A_answer_accuracy", "A. Answer accuracy (answerable attempts)", "pct"),
    ("A_answer_accuracy_majority_vote", "A'. Answer accuracy (per-question majority vote)", "pct"),
    ("B_safe_outcome_rate", "B. Correct safe-outcome rate", "pct"),
    ("C_false_confident_rate_safe_cases", "C. False-confident answers (safe cases)", "pct"),
    ("C2_wrong_answer_share_of_answers", "C2. Wrong share of ANSWER outcomes (answerable)", "pct"),
    ("D_unnecessary_abstention_rate", "D. Unnecessary abstention (answerable)", "pct"),
    ("execution_error_rate", "Execution-error rate", "pct"),
    ("E_sql_execution_success_rate", "E. SQL execution success (run_query calls)", "pct"),
    ("F_consistency_same_correctness", "F. Consistency across repetitions (correctness)", "pct"),
    ("F2_consistency_same_outcome", "F2. Consistency across repetitions (outcome)", "pct"),
    ("G_latency_ms_median", "G. Latency median (ms)", "num"),
    ("G_latency_ms_p95", "G. Latency p95 (ms)", "num"),
    ("H_tokens_per_attempt_mean", "H. Tokens per attempt (mean, incl. cache)", "num"),
    ("I_cost_usd_total", "I. Estimated cost total (USD)", "usd"),
    ("I_cost_usd_per_attempt", "I. Estimated cost per attempt (USD)", "usd"),
    ("I_cost_usd_per_correct_answer", "I. Estimated cost per correct answer (USD)", "usd"),
    ("J_context_retrieval_success", "J. Context retrieval success", "pct"),
    ("K_invalid_tool_call_rate", "K. Invalid tool-call rate", "pct"),
    ("L_privacy_violations", "L. Privacy violations", "int"),
    ("L_security_violations", "L. Security violations", "int"),
    ("N_tool_calls_mean", "N. Tool calls per attempt (mean)", "num"),
    ("O_sql_correction_attempts_mean", "O. SQL correction attempts (mean)", "num"),
]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{100 * v:.1f}%"
    if kind == "usd":
        return f"${v:.4f}"
    if kind == "int":
        return str(int(v))
    return f"{v:,.1f}"


def _banner(run: dict[str, Any]) -> list[str]:
    lines = []
    if not run["provider_live"]:
        lines.append(
            "> **NOT A MODEL EVALUATION.** This run used a test double (scripted provider) to exercise the "
            "harness. Its scores say nothing about model or context quality."
        )
    if run["status"] != "COMPLETE":
        lines.append(
            f"> **Run status: {run['status']}** - {run.get('status_reason') or ''}. Missing attempts are "
            "not counted as successes; metrics cover executed attempts only."
        )
    gold = run.get("gold") or {}
    counts = gold.get("status_counts", {})
    if counts.get("human_reviewed", 0) == 0:
        lines.append(
            "> **Gold labels are NOT human-reviewed** (status counts: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            + "). Scores are against SQL-validated, AI-authored gold and are not a verified benchmark."
        )
    return lines


def render_markdown(run: dict[str, Any], summary: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    modes = [m for m in MODE_ORDER if m in summary["modes"]] + sorted(set(summary["modes"]) - set(MODE_ORDER))
    prov = run.get("provenance") or {}
    out = [f"# Evaluation run {run['run_id']}", ""]
    out += [*_banner(run), ""]
    resp = [a["response"] for a in attempts if isinstance(a["response"], dict)]
    ctx_versions = sorted({str(r["context_version"]) for r in resp if r.get("context_version")})
    prompt_hashes = sorted({str(r["prompt_hash"]) for r in resp if r.get("prompt_hash")})
    out += [
        "## Run",
        f"- kind: {run['kind']}  label: {run.get('label') or '-'}  status: **{run['status']}**",
        f"- attempts: {run['executed_attempts']} executed / {run['planned_attempts']} planned",
        f"- provider: {run['provider']} (live={bool(run['provider_live'])}), requested model: `{run['requested_model']}`",
        f"- resolved model ids reported by provider: {summary['overall']['resolved_models'] or 'n/a'}",
        f"- agent config: `{json.dumps(run['agent_config'], sort_keys=True)}`",
        f"- code: {prov.get('code')}",
        f"- dataset snapshot: `{(prov.get('dataset') or {}).get('snapshot_id')}` "
        f"(serving DB file sha256 `{(prov.get('dataset') or {}).get('serving_db_sha256')}`)",
        f"- context versions seen: {ctx_versions}",
        f"- prompt hashes seen: {prompt_hashes}",
        f"- held-out lock: {(run.get('gold') or {}).get('heldout_lock')}",
        f"- estimated cost: ${run['cost_usd']:.4f} (pricing: provider-reported usage x documented list prices)",
        "",
        "## Scorecard by context mode",
        "",
        "| Metric | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for key, label, kind in METRIC_ROWS:
        out.append(
            f"| {label} | " + " | ".join(_fmt(summary["modes"][m].get(key), kind) for m in modes) + " |"
        )
    out.append(
        "| attempts (answerable / safe) | "
        + " | ".join(
            f"{summary['modes'][m]['answerable_attempts']} / {summary['modes'][m]['safe_attempts']}"
            for m in modes
        )
        + " |"
    )
    if summary["comparisons"]:
        out += ["", "## Paired context comparisons (unit = question; repetitions averaged first)", ""]
        out += [
            "| Comparison | n | A | B | diff (pp) | relative | 95% bootstrap CI (pp) "
            "| improved / worse / same | McNemar p | Holm reject |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for key, c in summary["comparisons"].items():
            a = c["answer_accuracy"]
            if not a.get("n_questions"):
                continue
            ci = a["bootstrap_ci95_pp"]
            h = summary["significance_holm"].get(key, {})
            rel = "n/a" if a["relative_pct"] is None else f"{a['relative_pct']:+.1f}%"
            out.append(
                f"| {key} answer accuracy | {a['n_questions']} | {100 * a['score_a']:.1f}% | {100 * a['score_b']:.1f}% | "
                f"{a['absolute_pp']:+.1f} | {rel} | [{ci[0]:+.1f}, {ci[1]:+.1f}] | "
                f"{a['questions_improved']} / {a['questions_worse']} / {a['questions_unchanged']} | "
                f"{a['mcnemar_exact']['p_value']:.4f} | {h.get('reject_h0', 'n/a')} |"
            )
            s = c["safe_outcome_rate"]
            if s.get("n_questions"):
                ci = s["bootstrap_ci95_pp"]
                out.append(
                    f"| {key} safe-outcome rate | {s['n_questions']} | {100 * s['score_a']:.1f}% | {100 * s['score_b']:.1f}% | "
                    f"{s['absolute_pp']:+.1f} | - | [{ci[0]:+.1f}, {ci[1]:+.1f}] | "
                    f"{s['questions_improved']} / {s['questions_worse']} / {s['questions_unchanged']} | - | - |"
                )
        out += ["", "Cost/latency trade-off per comparison (A vs B):", ""]
        for key, c in summary["comparisons"].items():
            out.append(f"- {key}: {json.dumps(c['cost_latency'])}")
    out += ["", "## Per-category success rate", ""]
    cats = sorted({cat for m in modes for cat in summary["per_category"][m]})
    out += ["| Category | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    for cat in cats:
        cells = []
        for m in modes:
            d = summary["per_category"][m].get(cat)
            cells.append("n/a" if d is None else f"{_fmt(d['success_rate'], 'pct')} ({d['attempts']})")
        out.append(f"| {cat} | " + " | ".join(cells) + " |")
    out += ["", "## Repetition variability", ""]
    for m in modes:
        out.append(f"- {m}: {json.dumps(summary['repetition_variability'][m])}")
    out += ["", "## Outcome labels and error codes", ""]
    for m in modes:
        out.append(
            f"- {m}: labels {summary['modes'][m]['labels']}; errors {summary['modes'][m]['error_codes']}"
        )
    fails = [a for a in attempts if not a["correct"]][:12]
    if fails:
        out += ["", "## Failure examples (first 12)", ""]
        for a in fails:
            r = a["response"] if isinstance(a["response"], dict) else {}
            out += [
                f"### {a['case_id']} / {a['mode']} / rep {a['repetition']}: {a['label']}",
                f"- reason: {a['score'].get('reason')}",
                f"- outcome: {a['outcome']}; answer: {(r.get('answer') or '')[:300]!r}",
                f"- generated SQL: `{(r.get('generated_sql') or '-')[:600]}`",
                f"- executed SQL: `{(r.get('executed_sql') or '-')[:600]}`",
                f"- context refs: metrics={r.get('referenced_metrics')}, terms={r.get('referenced_terms')}, "
                f"assets={r.get('referenced_assets')}",
                "",
            ]
    out += [
        "## Limitations",
        "- Gold SQL and the governed context were authored by the same AI agent; "
        "see EVALUATION_METHODOLOGY.md (threats to validity).",
        "- Unreviewed gold labels are not a verified benchmark.",
        "- Model behaviour is stochastic; small differences are within repetition noise unless the paired CI excludes 0.",
        "- Costs are estimates from provider-reported token usage and a dated list-price table.",
    ]
    return "\n".join(out) + "\n"


def write_run_artifacts(store: EvalStore, run_id: str, run_dir: Path) -> dict[str, Any]:
    run = store.run(run_id)
    if run is None:
        raise KeyError(run_id)
    attempts = store.attempts(run_id)
    summary = summarize(attempts)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"run": run, "summary": summary}
    (run_dir / "summary.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    (run_dir / "report.md").write_text(render_markdown(run, summary, attempts), encoding="utf-8")
    with open(run_dir / "attempts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "attempt_id",
                "case_id",
                "split",
                "mode",
                "repetition",
                "outcome",
                "label",
                "correct",
                "reason",
                "latency_ms",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "tool_calls",
                "sql_repairs",
                "invalid_tool_calls",
                "error_code",
                "trace_id",
                "generated_sql",
                "executed_sql",
            ]
        )
        for a in attempts:
            s, r = a["score"], a["response"] if isinstance(a["response"], dict) else {}
            w.writerow(
                [
                    a["attempt_id"],
                    a["case_id"],
                    a["split"],
                    a["mode"],
                    a["repetition"],
                    a["outcome"],
                    a["label"],
                    a["correct"],
                    s.get("reason"),
                    s.get("latency_ms"),
                    s.get("input_tokens"),
                    s.get("output_tokens"),
                    s.get("cost_usd"),
                    s.get("tool_calls"),
                    s.get("sql_repairs"),
                    s.get("invalid_tool_calls"),
                    s.get("error_code"),
                    a["trace_id"],
                    r.get("generated_sql"),
                    r.get("executed_sql"),
                ]
            )
    return payload


def compare_runs(store: EvalStore, run_a: str, run_b: str) -> dict[str, Any]:
    """Before/after comparison (e.g. a context change) per mode, paired by question."""
    ra, rb = store.run(run_a), store.run(run_b)
    if ra is None or rb is None:
        raise KeyError("unknown run id")
    aa, ab = store.attempts(run_a), store.attempts(run_b)
    out: dict[str, Any] = {
        "run_a": {k: ra[k] for k in ("run_id", "status", "provider_live", "requested_model")},
        "run_b": {k: rb[k] for k in ("run_id", "status", "provider_live", "requested_model")},
        "warnings": [],
        "modes": {},
    }
    if ra["requested_model"] != rb["requested_model"]:
        out["warnings"].append("different models: differences are not attributable to context alone")
    if ra["agent_config"] != rb["agent_config"]:
        out["warnings"].append("different agent configurations")
    if not (ra["provider_live"] and rb["provider_live"]):
        out["warnings"].append("at least one run is not a live model evaluation")
    for mode in sorted({a["mode"] for a in aa} & {a["mode"] for a in ab}):
        xa = [a for a in aa if a["mode"] == mode]
        xb = [a for a in ab if a["mode"] == mode]
        out["modes"][mode] = {
            "answer_accuracy": paired_comparison(xa, xb, answerable=True),
            "safe_outcome_rate": paired_comparison(xa, xb, answerable=False),
        }
    return out
