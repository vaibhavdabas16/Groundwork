"""Operational SQLite database (``data/ops/product.sqlite``) with forward-only migrations.

One file, WAL mode, a busy timeout and short transactions: adequate for a 5-10 user pilot on one
VM. Every connection enables foreign keys. Migrations are numbered, applied once, recorded in
``schema_migrations`` and never edited after release (add a new one instead).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE slack_events (
    dedupe_key TEXT PRIMARY KEY,           -- event_id / action payload id; retries reuse it
    kind TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,           -- == agent trace id
    team_id TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL, message_ts TEXT,
    slack_event_id TEXT,
    question TEXT NOT NULL,
    conversation_id TEXT,
    principal TEXT NOT NULL,
    delivery TEXT NOT NULL,                -- live | shadow
    status TEXT NOT NULL,                  -- QUEUED RUNNING DONE FAILED INTERRUPTED CANCELLED
    outcome TEXT, error_code TEXT, failure_category TEXT,
    context_version TEXT, model TEXT,
    latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    status_message_ts TEXT,
    delivered_at TEXT,                     -- set once the answer reached the user
    response_json TEXT,                    -- AgentResponse (preview rows only; no full results)
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX requests_event ON requests(slack_event_id) WHERE slack_event_id IS NOT NULL;
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL, user_id TEXT NOT NULL,
    state TEXT NOT NULL,                   -- AWAITING_CLARIFICATION RESOLVED EXPIRED CANCELLED STALE
    original_question TEXT NOT NULL,
    clarification_question TEXT,
    turns INTEGER NOT NULL DEFAULT 0,
    context_version TEXT,
    selected_definitions TEXT NOT NULL DEFAULT '[]',
    last_request_id TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX conversations_open ON conversations(team_id, channel_id, thread_ts, user_id)
    WHERE state = 'AWAITING_CLARIFICATION';
CREATE TABLE consents (
    team_id TEXT NOT NULL, user_id TEXT NOT NULL,
    notice_version TEXT NOT NULL, decision TEXT NOT NULL, decided_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id, notice_version)
);
CREATE TABLE feedback (
    feedback_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
    user_id TEXT NOT NULL, rating TEXT NOT NULL, category TEXT, comment TEXT, created_at TEXT NOT NULL,
    UNIQUE (request_id, user_id, rating)
);
CREATE TABLE failures (
    failure_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    question TEXT NOT NULL,
    safe_explanation TEXT NOT NULL,
    context_ids TEXT NOT NULL,
    evidence TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    remediation TEXT NOT NULL,
    context_fix_appropriate INTEGER NOT NULL,
    status TEXT NOT NULL,                  -- OPEN PROPOSED NOT_ACTIONABLE RESOLVED
    context_version TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE answer_reviews (                -- SHADOW MODE (answer approval; not context approval)
    review_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES requests(request_id),
    state TEXT NOT NULL,                   -- PENDING APPROVED REJECTED CORRECTED CLARIFICATION_REQUESTED
    reviewer_user_id TEXT, reviewer_note TEXT, review_message_ts TEXT,
    decided_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,          -- stable: derived from failure signature
    signature TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    category TEXT NOT NULL,
    failure_ids TEXT NOT NULL, trace_ids TEXT NOT NULL, eval_case_ids TEXT NOT NULL,
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,               -- ContextProposal for the current revision
    review_tier TEXT NOT NULL,             -- standard | elevated
    base_context_version TEXT NOT NULL,
    proposed_context_version TEXT,
    validation_json TEXT,
    author TEXT NOT NULL, author_type TEXT NOT NULL,
    pr_number INTEGER, pr_url TEXT, branch TEXT, head_sha TEXT, merge_commit TEXT,
    review_message_ts TEXT,
    decision TEXT, decided_by TEXT, decision_reason TEXT, decided_at TEXT, decided_revision INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE context_deployments (
    deployment_id TEXT PRIMARY KEY,
    context_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,                  -- path / git sha the release was built from
    git_commit TEXT,
    proposal_id TEXT,
    status TEXT NOT NULL,                  -- ACTIVE SUPERSEDED REJECTED ROLLED_BACK
    validation TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('human', 'system', 'agent')),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    prior_state TEXT, new_state TEXT,
    github_ref TEXT, slack_event_id TEXT, trace_id TEXT, context_version TEXT,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
""",
    ),
]


class Database:
    """Thread-safe access to the operational DB (one connection per thread, short transactions)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrate_lock = threading.Lock()
        self.migrate()

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE transaction: serialises writers, so check-then-act sequences are atomic."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params))

    def one(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn().execute(sql, params).fetchone()
        return row

    def migrate(self) -> list[int]:
        applied: list[int] = []
        with self._migrate_lock:
            conn = self._conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            have = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in have:
                    continue
                conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, datetime('now'))",
                    (version,),
                )
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        row = self.one("SELECT max(version) FROM schema_migrations")
        return int(row[0]) if row and row[0] is not None else 0

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
