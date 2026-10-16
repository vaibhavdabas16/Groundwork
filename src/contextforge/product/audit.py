"""Durable, append-only audit event stream.

Guarantees (implemented and tested):
* Append-only at the application AND database level: SQLite triggers abort UPDATE/DELETE.
* Hash chain: every event stores ``prev_hash`` and ``hash = sha256(prev_hash || canonical event)``.
  ``verify()`` recomputes the chain and reports the first broken link, a sequence gap, or a
  reordered row. This DETECTS accidental edits, deletions made with the triggers dropped and
  truncation of the tail when compared with an exported head hash. It is NOT tamper-proof against
  someone with write access to the file who rewrites the whole chain; export ``head()`` to a
  separate system (e.g. the pilot report or a log sink) if that matters.
* Events are written in the same transaction as the state change they describe when the caller
  passes its transaction connection (``conn=``), so a state change cannot commit without its event.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from contextforge.product.db import Database

ActorType = Literal["human", "system", "agent"]
GENESIS = "0" * 64

ACTIONS = frozenset(
    {
        "question.received",
        "question.rejected",
        "agent.executed",
        "policy.decision",
        "failure.classified",
        "proposal.created",
        "proposal.transition",
        "proposal.revised",
        "pr.created",
        "pr.updated",
        "pr.merged",
        "pr.closed",
        "validation.performed",
        "review.requested",
        "approval.granted",
        "approval.rejected",
        "revision.requested",
        "approval.denied_unauthorized",
        "approval.denied_stale",
        "answer.review_requested",
        "answer.approved",
        "answer.rejected",
        "answer.corrected",
        "answer.clarification_requested",
        "answer.delivered",
        "context.deployed",
        "context.deploy_rejected",
        "context.rollback",
        "regression.detected",
        "conversation.transition",
        "consent.recorded",
        "feedback.recorded",
        "slack.delivery_failed",
        "github.operation_failed",
        "request.interrupted",
    }
)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")


def _canonical(row: dict[str, Any]) -> str:
    keys = [
        "event_id",
        "ts",
        "actor_id",
        "actor_type",
        "action",
        "resource_type",
        "resource_id",
        "prior_state",
        "new_state",
        "github_ref",
        "slack_event_id",
        "trace_id",
        "context_version",
        "details",
    ]
    return json.dumps({k: row.get(k) for k in keys}, sort_keys=True, separators=(",", ":"))


def _hash(prev: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev + "|" + _canonical(row)).encode()).hexdigest()


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    events: int
    head: str
    problem: str | None = None
    at_seq: int | None = None


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self,
        *,
        action: str,
        actor_id: str,
        actor_type: ActorType,
        resource_type: str,
        resource_id: str,
        prior_state: str | None = None,
        new_state: str | None = None,
        github_ref: str | None = None,
        slack_event_id: str | None = None,
        trace_id: str | None = None,
        context_version: str | None = None,
        details: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        if action not in ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        row: dict[str, Any] = {
            "event_id": uuid.uuid4().hex,
            "ts": _now(),
            "actor_id": actor_id,
            "actor_type": actor_type,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "prior_state": prior_state,
            "new_state": new_state,
            "github_ref": github_ref,
            "slack_event_id": slack_event_id,
            "trace_id": trace_id,
            "context_version": context_version,
            "details": json.dumps(details or {}, sort_keys=True, default=str),
        }

        def write(c: sqlite3.Connection) -> None:
            last = c.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last[0] if last else GENESIS
            row["prev_hash"] = prev
            row["hash"] = _hash(prev, row)
            cols = ", ".join(row)
            c.execute(
                f"INSERT INTO audit_events ({cols}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )

        if conn is not None:
            write(conn)
        else:
            with self.db.tx() as c:
                write(c)
        return str(row["event_id"])

    def events(
        self, *, resource_id: str | None = None, action: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if resource_id:
            where.append("resource_id = ?")
            params.append(resource_id)
        if action:
            where.append("action = ?")
            params.append(action)
        sql = "SELECT * FROM audit_events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY seq LIMIT ?"
        params.append(str(limit))
        out = []
        for r in self.db.query(sql, tuple(params)):
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out

    def head(self) -> str:
        r = self.db.one("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        return str(r[0]) if r else GENESIS

    def verify(self, expected_head: str | None = None) -> ChainReport:
        prev = GENESIS
        n = 0
        last_seq = 0
        for r in self.db.query("SELECT * FROM audit_events ORDER BY seq"):
            row = dict(r)
            n += 1
            if row["seq"] != last_seq + 1:
                return ChainReport(False, n, prev, "sequence gap (event deleted?)", row["seq"])
            last_seq = row["seq"]
            if row["prev_hash"] != prev:
                return ChainReport(False, n, prev, "prev_hash mismatch", row["seq"])
            if _hash(prev, row) != row["hash"]:
                return ChainReport(False, n, prev, "hash mismatch (event modified)", row["seq"])
            prev = row["hash"]
        if expected_head is not None and expected_head != prev:
            return ChainReport(False, n, prev, "head differs from the exported head (truncated?)")
        return ChainReport(True, n, prev)
