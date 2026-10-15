"""Unit tests: comparator semantics, case-set integrity, label isolation, statistics, gate rules."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest

from contextforge.eval.cases import Comparator, load_cases
from contextforge.eval.compare import compare_one, compare_to_expected
from contextforge.eval.gate import evaluate_gate, load_policy
from contextforge.eval.gold import GoldPaths, expected_hash, has_gold_sql, load_expected
from contextforge.eval.stats import holm, paired_comparison, summarize
from contextforge.eval.store import EvalStore

ROOT = Path(__file__).resolve().parents[2]
SC = Comparator(type="scalar")


# ------------------------------------------------------------------ comparator
def test_scalar_matches_any_column_with_tolerance_and_types() -> None:
    assert compare_one(["n"], [[625]], ["status", "n"], [["canceled", "625"]], SC).match
    assert compare_one(["v"], [[16008872.12]], ["x"], [[16008872.12000001]], SC).match
    assert not compare_one(["v"], [[100.0]], ["x"], [[100.02]], SC).match
    assert not compare_one(["n"], [[625]], ["n"], [[625], [1]], SC).match  # extra rows fail
    assert not compare_one(["n"], [[625]], ["n"], [], SC).match


def test_percent_scaling_only_when_enabled() -> None:
    assert not compare_one(["r"], [[0.0312]], ["p"], [[3.12]], SC).match
    assert compare_one(
        ["r"], [[0.0312]], ["p"], [[3.12]], Comparator(type="scalar", percent_scaling=True)
    ).match


def test_rows_unordered_column_mapping_by_value_and_extra_columns() -> None:
    c = Comparator(type="rows")
    gold_cols, gold = ["state", "n"], [["SP", 10], ["RJ", 5]]
    assert compare_one(gold_cols, gold, ["cnt", "uf", "extra"], [[5, "rj", "x"], [10, "SP", "y"]], c).match
    assert not compare_one(
        gold_cols, gold, ["uf", "cnt"], [["SP", 5], ["RJ", 10]], c
    ).match  # rows misaligned
    assert not compare_one(gold_cols, gold, ["uf", "cnt"], [["SP", 10]], c).match  # missing row


def test_rows_ordered_and_month_normalisation_and_nulls() -> None:
    c = Comparator(type="rows", ordered=True, month_columns=["m"])
    assert compare_one(
        ["m", "n"],
        [["2018-01", 1], ["2018-02", 2]],
        ["month", "n"],
        [["2018-01-01", 1], ["2018-02-01T00:00:00", 2]],
        c,
    ).match
    assert not compare_one(
        ["m", "n"], [["2018-01", 1], ["2018-02", 2]], ["m", "n"], [["2018-02", 2], ["2018-01", 1]], c
    ).match
    assert compare_one(["a"], [[None]], ["a"], [[None]], Comparator(type="rows")).match


def test_column_subset_and_alternatives() -> None:
    c = Comparator(type="rows", columns=["category"])
    exp = {
        "columns": ["category", "sales"],
        "rows": [["a", 1.0], ["b", 2.0]],
        "alternatives": [
            {"label": "alternative_1", "columns": ["category", "sales"], "rows": [["x", 1.0], ["y", 2.0]]}
        ],
    }
    r = compare_to_expected(exp, ["cat", "total"], [["b", 99.0], ["a", 98.0]], c)
    assert r.match and r.matched_label == "primary"
    r = compare_to_expected(exp, ["cat"], [["y"], ["x"]], c)
    assert r.match and r.matched_label == "alternative_1"


# ------------------------------------------------------------------ case set
def test_case_set_size_split_and_distribution() -> None:
    cases = load_cases(GoldPaths(ROOT).cases)
    assert 80 <= len(cases) <= 100
    split = Counter(c.split for c in cases)
    assert split == {"dev": 20, "heldout": 70}
    cats = Counter(c.category for c in cases)
    assert len(cats) >= 18 and max(cats.values()) <= 10  # not padded with one category
    assert sum(c.answerable for c in cases) >= 60 and sum(not c.answerable for c in cases) >= 15
    # every case with gold SQL has a frozen expected result for its current content
    for c in cases:
        if has_gold_sql(c):
            exp = load_expected(GoldPaths(ROOT), c.id)
            assert exp is not None and exp["case_hash"] == c.content_hash(), c.id
            assert exp["rows"], c.id


def test_heldout_lock_matches_committed_cases() -> None:
    lock = json.loads((GoldPaths(ROOT).lock).read_text())
    cases = {c.id: c for c in load_cases(GoldPaths(ROOT).cases)}
    for cid, fp in lock["cases"].items():
        c = cases[cid]
        assert fp["case_hash"] == c.content_hash(), f"{cid} changed after lock - re-lock with a logged reason"
        if has_gold_sql(c):
            exp = load_expected(GoldPaths(ROOT), cid)
            assert exp is not None and fp["expected_sha256"] == expected_hash(exp)


def test_agent_code_cannot_see_labels() -> None:
    """The agent package must not import the eval package or read evals/ files."""
    agent_dir = ROOT / "src" / "contextforge" / "agent"
    for py in agent_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("contextforge.eval"), py
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "evals/" not in node.value and "gold_sql" not in node.value, py


def test_prompt_has_no_dataset_specific_semantics() -> None:
    from contextforge.agent.prompts import SYSTEM_PROMPT, user_message

    text = (SYSTEM_PROMPT + user_message("x")).lower()
    for forbidden in (
        "olist",
        "analytics.",
        "order_items",
        "payment",
        "customer_unique",
        "2018",
        "boleto",
        "freight",
    ):
        assert forbidden not in text, forbidden


# ------------------------------------------------------------------ statistics
def _att(case: str, mode: str, rep: int, correct: bool, answerable: bool = True) -> dict:
    return {
        "case_id": case,
        "mode": mode,
        "repetition": rep,
        "correct": int(correct),
        "split": "heldout",
        "score": {
            "case_id": case,
            "answerable": answerable,
            "category": "basic_count",
            "outcome": "ANSWER",
            "label": "correct" if correct else "wrong_answer",
            "correct": correct,
            "false_confident": False,
            "unnecessary_abstention": False,
            "execution_error": False,
            "error_code": None,
            "run_query_calls": 1,
            "run_query_ok": 1,
            "sql_repairs": 0,
            "tool_calls": 3,
            "invalid_tool_calls": 0,
            "retrieval_success": True,
            "privacy_violations": [],
            "security_violations": [],
            "latency_ms": 1000.0,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.01,
            "model_calls": 2,
            "resolved_models": ["m"],
        },
        "response": {},
    }


def test_paired_bootstrap_is_question_level() -> None:
    a = [_att(f"q{i}", "raw_schema", r, i < 4) for i in range(10) for r in (1, 2, 3)]
    b = [_att(f"q{i}", "full_context", r, i < 8) for i in range(10) for r in (1, 2, 3)]
    cmp = paired_comparison(a, b)
    assert cmp["n_questions"] == 10  # not 30: repetitions are averaged per question
    assert cmp["absolute_pp"] == pytest.approx(40.0)
    assert cmp["relative_pct"] == pytest.approx(100.0)
    lo, hi = cmp["bootstrap_ci95_pp"]
    assert lo > 0 and hi <= 100
    assert cmp["mcnemar_exact"]["b_correct_a_wrong"] == 4 and cmp["mcnemar_exact"]["a_correct_b_wrong"] == 0
    assert cmp["mcnemar_exact"]["p_value"] == pytest.approx(0.125)
    s = summarize(a + b)
    assert s["modes"]["full_context"]["A_answer_accuracy"] == pytest.approx(0.8)
    assert s["modes"]["raw_schema"]["F_consistency_same_correctness"] == 1.0


def test_holm_correction() -> None:
    h = holm({"x": 0.01, "y": 0.04, "z": 0.03})
    assert h["x"]["reject_h0"] and not h["y"]["reject_h0"] and not h["z"]["reject_h0"]


# ------------------------------------------------------------------ gate
def test_gate_blocks_without_live_reviewed_run(tmp_path: Path) -> None:
    policy = load_policy(ROOT / "evals" / "regression_policy.yaml")
    store = EvalStore(tmp_path / "e.sqlite")
    assert evaluate_gate(store, None, policy)["verdict"] == "BLOCKED"
    store.create_run(
        run_id="r1",
        status="COMPLETE",
        kind="experiment",
        provider="scripted-test",
        provider_live=0,
        requested_model="scripted-test",
        plan={},
        agent_config={},
        provenance={},
        gold={"heldout_lock": {"locked": True, "matches": True}, "case_statuses": {"q0": "sql_validated"}},
        planned_attempts=1,
    )
    store.add_attempt(
        "r1",
        0.0,
        attempt_id="a1",
        case_id="q0",
        split="heldout",
        mode="full_context",
        repetition=1,
        trace_id="t",
        started_at=0,
        finished_at=1,
        outcome="ANSWER",
        label="correct",
        correct=1,
        cost_usd=0.0,
        score=_att("q0", "full_context", 1, True)["score"],
        response={},
    )
    res = evaluate_gate(store, "r1", policy)
    assert res["verdict"] == "BLOCKED"
    assert "provider_live" in res["blocked"] and "enough_reviewed_heldout_cases" in res["blocked"]
    assert res["exit_code"] == 3
