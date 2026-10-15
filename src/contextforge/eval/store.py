"""Evaluation run/attempt storage (SQLite). Runs are append-only; nothing is ever overwritten."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,           -- RUNNING | COMPLETE | INCOMPLETE | BLOCKED | FAILED
    status_reason TEXT,
    kind TEXT NOT NULL,             -- experiment | smoke | regression | replay | plumbing_test
    label TEXT,
    provider TEXT,
    provider_live INTEGER,
    requested_model TEXT,
    plan TEXT NOT NULL,
    agent_config TEXT NOT NULL,
    provenance TEXT NOT NULL,
    gold TEXT NOT NULL,
    planned_attempts INTEGER NOT NULL,
    executed_attempts INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    replay_of TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    case_id TEXT NOT NULL,
    split TEXT NOT NULL,
    mode TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    outcome TEXT NOT NULL,
    label TEXT NOT NULL,
    correct INTEGER NOT NULL,
    cost_usd REAL,
    score TEXT NOT NULL,
    response TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_run ON attempts(run_id);
"""


class EvalStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)

    def create_run(self, **fields: Any) -> None:
        row = {
            k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()
        }
        row.setdefault("created_at", time.time())
        cols = ", ".join(row)
        with self._lock:
            self._con.execute(
                f"INSERT INTO runs ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
            )
            self._con.commit()

    def finish_run(self, run_id: str, status: str, reason: str | None) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE runs SET status = ?, status_reason = ?, finished_at = ? WHERE run_id = ?",
                (status, reason, time.time(), run_id),
            )
            self._con.commit()

    def add_attempt(self, run_id: str, cost: float | None, **fields: Any) -> None:
        row = {
            k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()
        }
        row["run_id"] = run_id
        cols = ", ".join(row)
        with self._lock:
            self._con.execute(
                f"INSERT INTO attempts ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
            )
            self._con.execute(
                "UPDATE runs SET executed_attempts = executed_attempts + 1, cost_usd = cost_usd + ? WHERE run_id = ?",
                (cost or 0.0, run_id),
            )
            self._con.commit()

    def run(self, run_id: str) -> dict[str, Any] | None:
        cur = self._con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        r = cur.fetchone()
        if r is None:
            return None
        d = dict(zip([c[0] for c in cur.description], r, strict=True))
        for k in ("plan", "agent_config", "provenance", "gold"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._con.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY case_id, mode, repetition", (run_id,)
        )
        names = [c[0] for c in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(names, r, strict=True))
            d["score"] = json.loads(d["score"])
            d["response"] = json.loads(d["response"])
            out.append(d)
        return out

    def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        cur = self._con.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))
        r = cur.fetchone()
        if r is None:
            return None
        d = dict(zip([c[0] for c in cur.description], r, strict=True))
        d["score"] = json.loads(d["score"])
        d["response"] = json.loads(d["response"])
        return d

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self._con.execute(
            "SELECT run_id, created_at, status, kind, label, provider, provider_live, requested_model, "
            "planned_attempts, executed_attempts, cost_usd FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._con.close()
