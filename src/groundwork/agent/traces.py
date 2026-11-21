"""Persistent agent traces (SQLite). Operational metadata only - never raw result rows.

Stored per request: ids (trace/request, eval run, case, attempt, replay-of), experiment mode,
prompt version/hash, code version, context version, dataset checksum, provider, requested and
resolved model ids, model parameters, tool calls (arguments + result summaries), generated and
executed SQL, query timings, outcome, errors, latency, token usage, cost estimate + pricing version.
Result values are represented by column names, row count and a SHA-256 of the canonical result.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from groundwork.agent.schemas import AgentResponse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    eval_run_id TEXT,
    case_id TEXT,
    attempt_id TEXT,
    replay_of TEXT,
    context_mode TEXT NOT NULL,
    question TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT,
    prompt_version TEXT,
    prompt_hash TEXT,
    code_git_sha TEXT,
    code_source_hash TEXT,
    context_version TEXT,
    dataset_sha256 TEXT,
    provider TEXT,
    provider_live INTEGER,
    requested_model TEXT,
    resolved_models TEXT,
    model_parameters TEXT,
    agent_config TEXT,
    tool_calls TEXT,
    queries TEXT,
    generated_sql TEXT,
    executed_sql TEXT,
    result_sha256 TEXT,
    result_row_count INTEGER,
    referenced TEXT,
    latency_ms REAL,
    usage TEXT,
    cost_usd REAL,
    pricing_version TEXT,
    comparison TEXT,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_traces_run ON traces(eval_run_id);
CREATE INDEX IF NOT EXISTS ix_traces_case ON traces(case_id);
"""


class TraceStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)

    def record(
        self,
        resp: AgentResponse,
        *,
        provenance: dict[str, Any],
        agent_config: dict[str, Any],
        result_sha256: str | None,
        eval_run_id: str | None = None,
        case_id: str | None = None,
        attempt_id: str | None = None,
        replay_of: str | None = None,
        comparison: dict[str, Any] | None = None,
    ) -> None:
        # The persisted response drops the result preview (values) and keeps shape only.
        stored = resp.model_dump(mode="json")
        stored["result_preview"] = []
        row = {
            "trace_id": resp.trace_id,
            "created_at": time.time(),
            "eval_run_id": eval_run_id,
            "case_id": case_id,
            "attempt_id": attempt_id,
            "replay_of": replay_of,
            "context_mode": resp.context_mode,
            "question": resp.question,
            "outcome": resp.outcome.value,
            "error_code": resp.error.code if resp.error else None,
            "prompt_version": resp.prompt_version,
            "prompt_hash": resp.prompt_hash,
            "code_git_sha": provenance.get("code", {}).get("git_sha"),
            "code_source_hash": provenance.get("code", {}).get("source_hash"),
            "context_version": resp.context_version,
            "dataset_sha256": "{};serving_db={}".format(
                provenance.get("dataset", {}).get("snapshot_id"),
                provenance.get("dataset", {}).get("serving_db_sha256"),
            ),
            "provider": resp.model.provider,
            "provider_live": int(resp.model.live),
            "requested_model": resp.model.requested_model,
            "resolved_models": json.dumps(resp.model.resolved_models),
            "model_parameters": json.dumps(resp.model.parameters, default=str),
            "agent_config": json.dumps(agent_config, default=str),
            "tool_calls": json.dumps([t.model_dump(mode="json") for t in resp.tool_calls]),
            "queries": json.dumps([q.model_dump(mode="json") for q in resp.queries]),
            "generated_sql": resp.generated_sql,
            "executed_sql": resp.executed_sql,
            "result_sha256": result_sha256,
            "result_row_count": resp.result_row_count,
            "referenced": json.dumps(
                {
                    "assets": resp.referenced_assets,
                    "metrics": resp.referenced_metrics,
                    "terms": resp.referenced_terms,
                    "certification_status": resp.certification_status,
                }
            ),
            "latency_ms": resp.latency_ms,
            "usage": resp.usage.model_dump_json(),
            "cost_usd": resp.cost.estimated_usd,
            "pricing_version": resp.cost.pricing_version,
            "comparison": json.dumps(comparison, default=str) if comparison is not None else None,
            "response_json": json.dumps(stored, default=str),
        }
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self._lock:
            self._con.execute(f"INSERT OR REPLACE INTO traces ({cols}) VALUES ({qs})", list(row.values()))
            self._con.commit()

    def set_comparison(self, trace_id: str, comparison: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE traces SET comparison = ? WHERE trace_id = ?",
                (json.dumps(comparison, default=str), trace_id),
            )
            self._con.commit()

    def get(self, trace_id: str) -> dict[str, Any] | None:
        cur = self._con.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
        r = cur.fetchone()
        if r is None:
            return None
        return dict(zip([d[0] for d in cur.description], r, strict=True))

    def close(self) -> None:
        with self._lock:
            self._con.close()
