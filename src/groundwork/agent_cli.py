"""CLI handlers for Phase 2: ``ask`` and ``eval ...``. Registered by ``groundwork.cli``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from groundwork.config.settings import ContextMode, get_settings

EXIT_BLOCKED = 3


def _print(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def _config(args: argparse.Namespace) -> Any:
    from groundwork.agent.providers.registry import DEFAULT_MODELS
    from groundwork.agent.schemas import AgentConfig

    provider = args.provider
    kw: dict[str, Any] = {
        "provider": provider,
        "model": args.model or DEFAULT_MODELS.get(provider, ""),
        "effort": args.effort,
    }
    if getattr(args, "max_steps", None):
        kw["max_steps"] = args.max_steps
    return AgentConfig(**kw)


def _blocked(msg: str, missing: list[str] | None = None) -> int:
    _print({"status": "BLOCKED", "reason": msg, "missing_configuration": missing or []})
    return EXIT_BLOCKED


def cmd_ask(args: argparse.Namespace) -> int:
    from groundwork.agent.factory import default_trace_store, open_agent
    from groundwork.agent.providers.base import ProviderError, ProviderNotConfigured
    from groundwork.agent.providers.registry import make_provider

    s = get_settings()
    cfg = _config(args)
    try:
        provider = make_provider(cfg)
    except ProviderNotConfigured as exc:
        return _blocked(exc.message, exc.missing)
    except ProviderError as exc:
        _print({"error": exc.kind, "message": exc.message})
        return 2

    async def main() -> Any:
        try:
            async with open_agent(
                s, cfg, mode=ContextMode(args.mode), provider=provider, trace_store=default_trace_store(s)
            ) as agent:
                return (await agent.ask(args.question)).response
        finally:
            await provider.aclose()

    resp = asyncio.run(main())
    sys.stdout.write(resp.model_dump_json(indent=2) + "\n")
    return 0 if resp.outcome.value != "EXECUTION_ERROR" else 1


def cmd_eval_run(args: argparse.Namespace) -> int:
    from groundwork.agent.providers.base import ProviderError, ProviderNotConfigured
    from groundwork.agent.providers.registry import make_provider
    from groundwork.eval.harness import EvalBlocked, RunPlan, run_experiment

    s = get_settings()
    modes = [
        ContextMode(m) for m in (["raw_schema", "descriptions", "full_context"] if args.all else args.mode)
    ]
    plan = RunPlan(
        modes=modes,
        split=args.split,
        case_ids=args.case_id or None,
        categories=args.category or None,
        repetitions=args.repetitions,
        concurrency=args.concurrency,
        budget_usd=args.budget_usd,
        kind=args.kind,
        label=args.label,
        max_cases=args.max_cases,
    )
    cfg = _config(args)
    try:
        provider = make_provider(cfg)
    except ProviderNotConfigured as exc:
        return _blocked(exc.message, exc.missing)
    except ProviderError as exc:
        _print({"error": exc.kind, "message": exc.message})
        return 2

    async def main() -> str:
        try:
            return await run_experiment(s, plan, cfg, provider)
        finally:
            await provider.aclose()

    try:
        run_id = asyncio.run(main())
    except EvalBlocked as exc:
        return _blocked(str(exc))
    return _report(run_id)


def _report(run_id: str) -> int:
    from groundwork.eval.harness import eval_paths
    from groundwork.eval.report import write_run_artifacts
    from groundwork.eval.store import EvalStore

    s = get_settings()
    db, runs = eval_paths(s)
    store = EvalStore(db)
    payload = write_run_artifacts(store, run_id, runs / run_id)
    run = payload["run"]
    _print(
        {
            "run_id": run_id,
            "status": run["status"],
            "status_reason": run["status_reason"],
            "live": bool(run["provider_live"]),
            "attempts": f"{run['executed_attempts']}/{run['planned_attempts']}",
            "artifacts": str(runs / run_id),
            "modes": {
                m: {k: v for k, v in sc.items() if k[0] in "ABCDEG" or k.startswith("L_")}
                for m, sc in payload["summary"]["modes"].items()
            },
        }
    )
    return 0 if run["status"] == "COMPLETE" else EXIT_BLOCKED


def cmd_eval_report(args: argparse.Namespace) -> int:
    return _report(args.run_id)


def cmd_eval_compare(args: argparse.Namespace) -> int:
    from groundwork.eval.harness import eval_paths
    from groundwork.eval.report import compare_runs
    from groundwork.eval.store import EvalStore

    out = compare_runs(EvalStore(eval_paths(get_settings())[0]), args.run_a, args.run_b)
    for m in out["modes"].values():
        for k in ("answer_accuracy", "safe_outcome_rate"):
            m[k].pop("per_question_diff", None)
    _print(out)
    return 0


def cmd_eval_replay(args: argparse.Namespace) -> int:
    """Re-run one recorded attempt with its recorded configuration. Always a NEW run (kind=replay)."""
    from groundwork.agent.providers.base import ProviderError, ProviderNotConfigured
    from groundwork.agent.providers.registry import make_provider
    from groundwork.agent.schemas import AgentConfig
    from groundwork.eval.harness import EvalBlocked, RunPlan, eval_paths, run_experiment
    from groundwork.eval.store import EvalStore

    s = get_settings()
    store = EvalStore(eval_paths(s)[0])
    att = store.attempt(args.attempt_id)
    if att is None:
        _print({"error": f"attempt {args.attempt_id} not found"})
        return 2
    run = store.run(att["run_id"])
    assert run is not None
    cfg = AgentConfig.model_validate(run["agent_config"])
    plan = RunPlan(
        modes=[ContextMode(att["mode"])],
        split="all",
        case_ids=[att["case_id"]],
        repetitions=args.repetitions,
        concurrency=1,
        kind="replay",
        label=f"replay of {args.attempt_id}",
        replay_of=args.attempt_id,
        allow_unlocked_heldout=True,
    )
    try:
        provider = make_provider(cfg)
    except ProviderNotConfigured as exc:
        return _blocked(exc.message, exc.missing)
    except ProviderError as exc:
        _print({"error": exc.kind, "message": exc.message})
        return 2

    async def main() -> str:
        try:
            return await run_experiment(s, plan, cfg, provider)
        finally:
            await provider.aclose()

    try:
        run_id = asyncio.run(main())
    except EvalBlocked as exc:
        return _blocked(str(exc))
    new = store.attempts(run_id)
    old_prompt = (att["response"] or {}).get("prompt_hash")
    _print(
        {
            "replay_run_id": run_id,
            "replay_of": args.attempt_id,
            "note": "This is a NEW run with the recorded configuration, not the original run.",
            "original": {"outcome": att["outcome"], "label": att["label"], "prompt_hash": old_prompt},
            "replay": [
                {
                    "outcome": a["outcome"],
                    "label": a["label"],
                    "prompt_hash": a["response"].get("prompt_hash"),
                }
                for a in new
            ],
            "prompt_changed": any(a["response"].get("prompt_hash") != old_prompt for a in new),
        }
    )
    return 0


def cmd_eval_list(args: argparse.Namespace) -> int:
    from groundwork.eval.harness import eval_paths
    from groundwork.eval.store import EvalStore

    _print(EvalStore(eval_paths(get_settings())[0]).list_runs(args.limit))
    return 0


def cmd_eval_gate(args: argparse.Namespace) -> int:
    from groundwork.eval.gate import evaluate_gate, load_policy
    from groundwork.eval.harness import eval_paths
    from groundwork.eval.store import EvalStore

    s = get_settings()
    policy = load_policy(Path(args.policy) if args.policy else s.root / "evals" / "regression_policy.yaml")
    res = evaluate_gate(
        EvalStore(eval_paths(s)[0]), args.run_id, policy, baseline_run_id=args.baseline_run_id
    )
    _print(res)
    return int(res["exit_code"])


def cmd_gold(args: argparse.Namespace) -> int:
    from groundwork.eval import gold

    s = get_settings()
    try:
        if args.gold_cmd == "freeze":
            m = gold.freeze(s)
            _print({"counts": m["counts"], "failures": m["failures"]})
            return 1 if m["failures"] else 0
        if args.gold_cmd == "check":
            res = gold.check(s)
            _print(res)
            return 0 if res["ok"] else 1
        if args.gold_cmd == "status":
            r = gold.status_report(s)
            if not args.verbose:
                r.pop("cases")
            _print(r)
            return 0
        if args.gold_cmd == "lock":
            lock = gold.lock_heldout(s, reason=args.reason)
            _print({k: lock[k] for k in ("locked_at", "case_count", "fingerprint_sha256")})
            return 0
        if args.gold_cmd == "verify":
            v = gold.verify_lock(s)
            _print(v)
            return 0 if v["matches"] else 1
        if args.gold_cmd == "review":
            _print(
                gold.record_review(
                    s, args.case_id, reviewer=args.reviewer, decision_ref=args.decision_ref, notes=args.notes
                )
            )
            return 0
    except gold.GoldError as exc:
        _print({"error": str(exc)})
        return 2
    return 2


def register(sub: Any) -> None:
    def model_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--provider",
            default=os.environ.get("GROUNDWORK_PROVIDER", "anthropic"),
            choices=["anthropic", "openai"],
            help="model provider (default: $GROUNDWORK_PROVIDER or anthropic); never switched automatically",
        )
        p.add_argument(
            "--model", default=None, help="model id (default: claude-opus-5-5 / gpt-5.5 by provider)"
        )
        p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--max-steps", type=int, default=None)

    a = sub.add_parser(
        "ask", help="ask the data agent a question (live model; needs ANTHROPIC_API_KEY or OPENAI_API_KEY)"
    )
    a.add_argument("question")
    a.add_argument("--mode", default="full_context", choices=[m.value for m in ContextMode])
    model_args(a)
    a.set_defaults(fn=cmd_ask)

    ev = sub.add_parser("eval", help="evaluation platform")
    es = ev.add_subparsers(dest="eval_cmd", required=True)
    r = es.add_parser("run", help="run the real agent on evaluation cases")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--mode", action="append", choices=[m.value for m in ContextMode])
    g.add_argument("--all", action="store_true", help="all three context modes")
    r.add_argument("--split", default="dev", choices=["dev", "heldout", "all"])
    r.add_argument("--case-id", action="append")
    r.add_argument("--category", action="append")
    r.add_argument("--repetitions", type=int, default=1)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--budget-usd", type=float, default=None)
    r.add_argument("--max-cases", type=int, default=None)
    r.add_argument("--kind", default="smoke", choices=["experiment", "smoke", "regression"])
    r.add_argument("--label")
    model_args(r)
    r.set_defaults(fn=cmd_eval_run)
    rp = es.add_parser("report", help="(re)generate JSON/Markdown/CSV artifacts for a run")
    rp.add_argument("--run-id", required=True)
    rp.set_defaults(fn=cmd_eval_report)
    c = es.add_parser("compare", help="paired before/after comparison of two runs")
    c.add_argument("--run-a", required=True)
    c.add_argument("--run-b", required=True)
    c.set_defaults(fn=cmd_eval_compare)
    rl = es.add_parser("replay", help="re-run a recorded attempt with its recorded configuration (new run)")
    rl.add_argument("--attempt-id", required=True)
    rl.add_argument("--repetitions", type=int, default=1)
    rl.set_defaults(fn=cmd_eval_replay)
    ls = es.add_parser("list", help="list evaluation runs")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(fn=cmd_eval_list)
    gt = es.add_parser("gate", help="regression gate (PASS=0, FAIL=1, BLOCKED=3)")
    gt.add_argument("--run-id")
    gt.add_argument("--baseline-run-id")
    gt.add_argument("--policy")
    gt.set_defaults(fn=cmd_eval_gate)
    gd = es.add_parser("gold", help="gold-label lifecycle")
    gs = gd.add_subparsers(dest="gold_cmd", required=True)
    gs.add_parser("freeze", help="execute gold SQL on the snapshot and write expected results")
    gs.add_parser("check", help="re-execute gold SQL and compare with committed expected results (no writes)")
    st = gs.add_parser("status", help="review/validation status of every case")
    st.add_argument("--verbose", action="store_true")
    lk = gs.add_parser("lock", help="lock the held-out split (re-lock needs --reason)")
    lk.add_argument("--reason")
    gs.add_parser("verify", help="check the held-out split still matches its lock")
    rv = gs.add_parser("review", help="record a HUMAN review of one case")
    rv.add_argument("--case-id", required=True)
    rv.add_argument("--reviewer", required=True, help="person.<id> listed in context/ownership/owners.yaml")
    rv.add_argument("--decision-ref", required=True)
    rv.add_argument("--notes")
    gd.set_defaults(fn=cmd_gold)
