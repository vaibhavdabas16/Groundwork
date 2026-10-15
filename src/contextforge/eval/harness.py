"""Evaluation harness: runs the REAL agent (real provider, real MCP server, governed SQL) per case.

Controls held constant across modes: model, AgentConfig (incl. limits/retries), prompt, tool
interfaces, principal/permissions, warehouse snapshot, scoring rules. Only the MCP server's
``CONTEXTFORGE_CONTEXT_MODE`` differs. Jobs (case x mode x repetition) are shuffled with a fixed
seed so no mode systematically runs at a different time. Every planned attempt either produces a
stored, scored attempt or leaves the run INCOMPLETE; unexecuted attempts are never counted.
No model response is cached or reused: each repetition is a new conversation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import secrets
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from contextforge.agent.factory import open_agent
from contextforge.agent.pricing import PRICES_FILE_ENV, has_price
from contextforge.agent.provenance import provenance
from contextforge.agent.providers.base import ModelProvider
from contextforge.agent.runtime import AgentResult, DataAgent
from contextforge.agent.schemas import AgentConfig
from contextforge.agent.traces import TraceStore
from contextforge.config.settings import ContextMode, Settings
from contextforge.eval.cases import EvalCase, load_cases
from contextforge.eval.gold import GoldPaths, case_status, has_gold_sql, load_expected, verify_lock
from contextforge.eval.scoring import AttemptScore, score_attempt
from contextforge.eval.sensitive import SensitiveScanner
from contextforge.eval.store import EvalStore
from contextforge.observability.logging import get_logger

log = get_logger("eval")
RunKind = Literal["experiment", "smoke", "regression", "replay", "plumbing_test"]


@dataclass
class RunPlan:
    modes: list[ContextMode]
    split: Literal["dev", "heldout", "all"] = "dev"
    case_ids: list[str] | None = None
    categories: list[str] | None = None
    repetitions: int = 1
    concurrency: int = 2
    budget_usd: float | None = None
    kind: RunKind = "smoke"
    label: str | None = None
    max_cases: int | None = None
    seed: int = 20261120
    replay_of: str | None = None
    principal: str = "principal.local_analyst"
    allow_unlocked_heldout: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["modes"] = [m.value for m in self.modes]
        return d


class EvalBlocked(RuntimeError):
    pass


@dataclass
class _Shared:
    spent: float = 0.0
    stop_reason: str | None = None
    executed: int = 0
    auth_failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def eval_paths(settings: Settings) -> tuple[Path, Path]:
    base = settings.data_dir / "evals"
    return base / "evals.sqlite", base / "runs"


def select_cases(settings: Settings, plan: RunPlan) -> list[EvalCase]:
    cases = load_cases(GoldPaths(settings.root).cases)
    if plan.split != "all":
        cases = [c for c in cases if c.split == plan.split]
    if plan.case_ids:
        wanted = set(plan.case_ids)
        unknown = wanted - {c.id for c in load_cases(GoldPaths(settings.root).cases)}
        if unknown:
            raise EvalBlocked(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in load_cases(GoldPaths(settings.root).cases) if c.id in wanted]
    if plan.categories:
        cases = [c for c in cases if c.category in set(plan.categories)]
    if plan.max_cases is not None:
        cases = cases[: plan.max_cases]
    if not cases:
        raise EvalBlocked("no cases selected")
    return cases


def gold_preflight(settings: Settings, cases: list[EvalCase], plan: RunPlan) -> dict[str, Any]:
    paths = GoldPaths(settings.root)
    snap = provenance(settings)["dataset"]["snapshot_id"]
    statuses: dict[str, str] = {}  # case id -> review status
    stale = []
    for c in cases:
        st: str = case_status(paths, c, snap)
        statuses[c.id] = st
        if c.answerable and st == "authored":
            stale.append(c.id)
    if stale:
        raise EvalBlocked(
            f"gold results missing or stale for {stale[:10]} (run `contextforge eval gold freeze`)"
        )
    lock = verify_lock(settings)
    uses_heldout = any(c.split == "heldout" for c in cases)
    if uses_heldout and not (lock["locked"] and lock["matches"]) and not plan.allow_unlocked_heldout:
        raise EvalBlocked(f"held-out set is not locked or has changed since locking: {lock}")
    counts: dict[str, int] = {}
    for st in statuses.values():
        counts[st] = counts.get(st, 0) + 1
    return {"status_counts": counts, "statuses": statuses, "heldout_lock": lock, "snapshot_sha256": snap}


async def run_experiment(
    settings: Settings,
    plan: RunPlan,
    config: AgentConfig,
    provider: ModelProvider,
    *,
    store: EvalStore | None = None,
    trace_store: TraceStore | None = None,
    scanner: SensitiveScanner | None = None,
) -> str:
    """Execute a run and return its id. Raises EvalBlocked before anything is recorded if the
    preconditions (gold, lock) are not met."""
    if plan.kind != "plumbing_test" and not provider.live:
        raise EvalBlocked("a non-live provider (test double) can only be used for kind=plumbing_test")
    if provider.live and plan.budget_usd is not None and not has_price(config.model):
        raise EvalBlocked(
            f"no price is configured for model {config.model!r}, so --budget-usd cannot be enforced; "
            f"supply verified prices via {PRICES_FILE_ENV} (see agent/pricing.py) or omit the budget"
        )
    cases = select_cases(settings, plan)
    gold = gold_preflight(settings, cases, plan)
    db_path, runs_dir = eval_paths(settings)
    store = store or EvalStore(db_path)
    trace_store = trace_store or TraceStore(settings.data_dir / "ops" / "agent_traces.sqlite")
    if scanner is None and settings.warehouse_path.is_file():
        scanner = SensitiveScanner.load(settings.warehouse_path)
    run_id = f"run-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(c, m, r) for c in cases for m in plan.modes for r in range(1, plan.repetitions + 1)]
    random.Random(plan.seed).shuffle(jobs)  # noqa: S311 - reproducible ordering, not crypto
    store.create_run(
        run_id=run_id,
        status="RUNNING",
        kind=plan.kind,
        label=plan.label,
        provider=provider.name,
        provider_live=int(provider.live),
        requested_model=config.model if provider.live else provider.name,
        plan=plan.to_dict(),
        agent_config=config.model_dump(mode="json"),
        provenance=provenance(settings) | {"provider_parameters": provider.parameters()},
        gold={k: v for k, v in gold.items() if k != "statuses"} | {"case_statuses": gold["statuses"]},
        planned_attempts=len(jobs),
        replay_of=plan.replay_of,
    )
    log.info("eval run started", extra={"run_id": run_id, "planned": len(jobs)})
    queue: asyncio.Queue[tuple[EvalCase, ContextMode, int]] = asyncio.Queue()
    for j in jobs:
        queue.put_nowait(j)
    shared = _Shared()
    expected_cache = {c.id: load_expected(GoldPaths(settings.root), c.id) for c in cases if has_gold_sql(c)}

    async def worker(wid: int) -> None:
        async with AsyncExitStack() as stack:
            agents: dict[ContextMode, DataAgent] = {}
            while True:
                try:
                    case, mode, rep = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                async with shared.lock:
                    if (
                        shared.stop_reason is None
                        and plan.budget_usd is not None
                        and shared.spent >= plan.budget_usd
                    ):
                        shared.stop_reason = (
                            f"cost budget reached (${shared.spent:.2f} >= ${plan.budget_usd:.2f})"
                        )
                    if shared.stop_reason is not None:
                        return
                attempt_id = f"{run_id}:{case.id}:{mode.value}:{rep}"
                started = time.time()
                result: AgentResult | None = None
                harness_error: str | None = None
                try:
                    if mode not in agents:
                        agents[mode] = await stack.enter_async_context(
                            open_agent(
                                settings,
                                config,
                                mode=mode,
                                principal=plan.principal,
                                provider=provider,
                                trace_store=trace_store,
                                ops_dir=run_dir / "mcp" / f"w{wid}-{mode.value}",
                            )
                        )
                    result = await agents[mode].ask(
                        case.question,
                        request_id=f"tr-{uuid.uuid4().hex}",
                        trace_meta={
                            "eval_run_id": run_id,
                            "case_id": case.id,
                            "attempt_id": attempt_id,
                            "replay_of": plan.replay_of,
                        },
                    )
                except Exception as exc:  # harness/MCP start failure: recorded as a failed attempt
                    harness_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.error(
                        "attempt failed in harness", extra={"attempt_id": attempt_id, "error": harness_error}
                    )
                    agents.pop(mode, None)
                score: AttemptScore
                if result is not None:
                    score = score_attempt(case, result, expected_cache.get(case.id), scanner)
                    response = result.response.model_dump(mode="json")
                    response["result_preview"] = []  # never persist result values
                    trace_id = result.response.trace_id
                    if trace_store is not None:
                        trace_store.set_comparison(trace_id, score.to_dict())
                else:
                    score = AttemptScore(
                        case_id=case.id,
                        answerable=case.answerable,
                        category=case.category,
                        outcome="EXECUTION_ERROR",
                        label="execution_error",
                        correct=False,
                        reason=harness_error or "unknown",
                        execution_error=True,
                        error_code="harness_error",
                    )
                    response = {"error": harness_error}
                    trace_id = "none"
                store.add_attempt(
                    run_id,
                    score.cost_usd,
                    attempt_id=attempt_id,
                    case_id=case.id,
                    split=case.split,
                    mode=mode.value,
                    repetition=rep,
                    trace_id=trace_id,
                    started_at=started,
                    finished_at=time.time(),
                    outcome=score.outcome,
                    label=score.label,
                    correct=int(score.correct),
                    cost_usd=score.cost_usd,
                    score=score.to_dict(),
                    response=response,
                )
                async with shared.lock:
                    shared.executed += 1
                    shared.spent += score.cost_usd or 0.0
                    if score.error_code in ("provider_auth", "provider_not_found", "provider_config"):
                        shared.auth_failures += 1
                        if shared.auth_failures >= 2:
                            shared.stop_reason = f"provider configuration/auth failure ({score.error_code})"

    try:
        await asyncio.gather(*(worker(i) for i in range(max(1, plan.concurrency))))
    except BaseException as exc:
        store.finish_run(run_id, "INCOMPLETE", f"interrupted: {type(exc).__name__}")
        raise
    if shared.stop_reason is not None or shared.executed < len(jobs):
        status, reason = "INCOMPLETE", shared.stop_reason or f"executed {shared.executed}/{len(jobs)}"
    else:
        status, reason = "COMPLETE", None
    store.finish_run(run_id, status, reason)
    from contextforge.eval.report import write_run_artifacts

    write_run_artifacts(store, run_id, run_dir)
    log.info("eval run finished", extra={"run_id": run_id, "status": status, "reason": reason})
    return run_id
