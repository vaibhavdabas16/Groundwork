"""Evaluation harness PLUMBING test with a scripted test double (kind=plumbing_test).

The script is question-agnostic (search, one fixed query, submit) - it exists only to drive the
harness end to end: real MCP servers per mode, real governed execution, real scoring against
frozen gold, storage, artifacts, INCOMPLETE handling. Its scores are meaningless and the report
must say so.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from groundwork.agent.providers.base import ModelTurn, TextPart, ToolResultPart
from groundwork.agent.providers.scripted import ScriptedProvider, turn
from groundwork.agent.schemas import AgentConfig
from groundwork.config.settings import ContextMode, Settings
from groundwork.eval.harness import EvalBlocked, RunPlan, eval_paths, run_experiment
from groundwork.eval.store import EvalStore

pytestmark = [pytest.mark.integration, pytest.mark.agent]


def generic_script(h: list[list[TextPart | ToolResultPart]]) -> ModelTurn:
    if len(h) == 1:
        return turn(("search_assets", {"query": "orders"}))
    if len(h) == 2:
        return turn(("run_query", {"sql": "SELECT count(*) AS n FROM analytics.orders"}))
    part = h[-1][0]
    assert isinstance(part, ToolResultPart)
    qid = json.loads(part.content)["untrusted_tool_output"]["query_id"]
    return turn(("submit_response", {"outcome": "ANSWER", "answer": "n", "query_id": qid}))


def test_harness_end_to_end_with_test_double(isolated_settings: Settings) -> None:
    plan = RunPlan(
        modes=list(ContextMode),
        case_ids=["cf-001", "cf-008", "cf-s01", "cf-s15"],
        split="all",
        repetitions=2,
        concurrency=2,
        kind="plumbing_test",
        label="pytest plumbing",
    )
    run_id = asyncio.run(
        run_experiment(isolated_settings, plan, AgentConfig(), ScriptedProvider(generic_script))
    )
    db, runs = eval_paths(isolated_settings)
    store = EvalStore(db)
    run = store.run(run_id)
    assert run is not None and run["status"] == "COMPLETE" and run["executed_attempts"] == 24
    assert run["provider_live"] == 0
    attempts = store.attempts(run_id)
    labels = {(a["case_id"], a["mode"]): a["label"] for a in attempts}
    # cf-001 (total orders) happens to equal the fixed query -> correct; cf-008 -> wrong answer;
    # safe cases answered -> false-confident. Scoring is real even though the "model" is not.
    assert all(labels[("cf-001", m.value)] == "correct" for m in ContextMode)
    assert all(labels[("cf-008", m.value)] == "wrong_answer" for m in ContextMode)
    assert all(labels[("cf-s15", m.value)] == "false_confident_answer" for m in ContextMode)
    report = (runs / run_id / "report.md").read_text(encoding="utf-8")
    assert "NOT A MODEL EVALUATION" in report and "NOT human-reviewed" in report
    summary = json.loads((runs / run_id / "summary.json").read_text())
    assert set(summary["summary"]["modes"]) == {"raw_schema", "descriptions", "full_context"}
    assert (
        summary["summary"]["comparisons"]["raw_schema->full_context"]["answer_accuracy"]["n_questions"] == 2
    )
    assert (runs / run_id / "attempts.csv").is_file()


def test_harness_refuses_test_double_for_real_runs_and_marks_incomplete(isolated_settings: Settings) -> None:
    plan = RunPlan(modes=[ContextMode.full_context], case_ids=["cf-001"], split="all", kind="smoke")
    with pytest.raises(EvalBlocked):
        asyncio.run(run_experiment(isolated_settings, plan, AgentConfig(), ScriptedProvider(generic_script)))
    plan = RunPlan(
        modes=[ContextMode.full_context],
        case_ids=["cf-001", "cf-003"],
        split="all",
        kind="plumbing_test",
        budget_usd=-1.0,
    )
    run_id = asyncio.run(
        run_experiment(isolated_settings, plan, AgentConfig(), ScriptedProvider(generic_script))
    )
    run = EvalStore(eval_paths(isolated_settings)[0]).run(run_id)
    assert run is not None and run["status"] == "INCOMPLETE" and run["executed_attempts"] == 0
    assert "budget" in run["status_reason"]


def test_held_out_requires_matching_lock(isolated_settings: Settings, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "root"
    shutil.copytree(isolated_settings.root / "evals", root / "evals")
    lock = json.loads((root / "evals" / "heldout_lock.json").read_text())
    first = next(iter(lock["cases"]))
    lock["cases"][first]["case_hash"] = "tampered"
    (root / "evals" / "heldout_lock.json").write_text(json.dumps(lock))
    s = isolated_settings.model_copy(update={"root": root})
    plan = RunPlan(modes=[ContextMode.full_context], case_ids=[first], split="all", kind="plumbing_test")
    with pytest.raises(EvalBlocked, match="not locked or has changed"):
        asyncio.run(run_experiment(s, plan, AgentConfig(), ScriptedProvider(generic_script)))
