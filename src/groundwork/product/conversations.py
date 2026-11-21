"""Bounded clarification state machine, scoped to (workspace, channel, thread, user).

States: AWAITING_CLARIFICATION -> RESOLVED | EXPIRED | CANCELLED | STALE.
* Only ONE open conversation per (team, channel, thread, user) (unique partial index).
* A reply from a different user in the same thread never continues someone else's conversation.
* The agent receives a bounded, structured continuation (original question + the clarification
  question + the user's reply), never the Slack history, and never previous model output as a
  definition. ``max_turns`` caps the loop.
* If the deployed context version changed since the clarification was asked, the conversation is
  STALE and the user is asked to restate (an outdated plan is never silently executed).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from groundwork.product.audit import AuditLog
from groundwork.product.db import Database

MAX_QUESTION_CHARS = 2000


@dataclass
class Conversation:
    conversation_id: str
    state: str
    original_question: str
    clarification_question: str | None
    turns: int
    context_version: str | None
    expires_at: str


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def compose_continuation(original: str, clarification_q: str, reply: str) -> str:
    parts = [
        f"Original question: {original.strip()}",
        f"Clarification that was asked: {clarification_q.strip()}",
        f"The user's answer: {reply.strip()}",
    ]
    text = "\n".join(parts)
    if len(text) > MAX_QUESTION_CHARS:
        budget = MAX_QUESTION_CHARS - len(parts[1]) - len(parts[2]) - 30
        parts[0] = f"Original question: {original.strip()[: max(200, budget)]}…"
        text = "\n".join(parts)[:MAX_QUESTION_CHARS]
    return text


class ConversationStore:
    def __init__(self, db: Database, audit: AuditLog, ttl_minutes: int, max_turns: int) -> None:
        self.db, self.audit, self.ttl, self.max_turns = db, audit, ttl_minutes, max_turns

    def _set(self, c: Any, cid: str, prior: str, new: str, **cols: Any) -> None:
        sets = ", ".join(["state = ?", "updated_at = ?"] + [f"{k} = ?" for k in cols])
        c.execute(
            f"UPDATE conversations SET {sets} WHERE conversation_id = ?",
            (new, _now().isoformat(), *cols.values(), cid),
        )
        self.audit.append(
            action="conversation.transition",
            actor_id="system.conversations",
            actor_type="system",
            resource_type="conversation",
            resource_id=cid,
            prior_state=prior,
            new_state=new,
            details={},
            conn=c,
        )

    def open_for(self, team: str, channel: str, thread: str, user: str) -> Conversation | None:
        r = self.db.one(
            "SELECT * FROM conversations WHERE team_id=? AND channel_id=? AND thread_ts=? AND user_id=? AND state='AWAITING_CLARIFICATION'",  # noqa: E501 - long literal
            (team, channel, thread, user),
        )
        if r is None:
            return None
        if dt.datetime.fromisoformat(r["expires_at"]) < _now():
            with self.db.tx() as c:
                self._set(c, r["conversation_id"], "AWAITING_CLARIFICATION", "EXPIRED")
            return None
        return Conversation(
            r["conversation_id"],
            r["state"],
            r["original_question"],
            r["clarification_question"],
            int(r["turns"]),
            r["context_version"],
            r["expires_at"],
        )

    def await_clarification(
        self,
        *,
        team: str,
        channel: str,
        thread: str,
        user: str,
        original: str,
        question: str,
        context_version: str | None,
        request_id: str,
        existing: Conversation | None,
    ) -> str:
        exp = (_now() + dt.timedelta(minutes=self.ttl)).isoformat()
        with self.db.tx() as c:
            if existing is not None:
                c.execute(
                    "UPDATE conversations SET clarification_question=?, turns=turns+1, expires_at=?, last_request_id=?, "
                    "context_version=?, updated_at=? WHERE conversation_id=?",
                    (
                        question,
                        exp,
                        request_id,
                        context_version,
                        _now().isoformat(),
                        existing.conversation_id,
                    ),
                )
                return existing.conversation_id
            cid = f"conv-{uuid.uuid4().hex[:12]}"
            now = _now().isoformat()
            c.execute(
                "INSERT INTO conversations (conversation_id, team_id, channel_id, thread_ts, user_id, state, original_question, "
                "clarification_question, turns, context_version, last_request_id, expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (
                    cid,
                    team,
                    channel,
                    thread,
                    user,
                    "AWAITING_CLARIFICATION",
                    original,
                    question,
                    context_version,
                    request_id,
                    exp,
                    now,
                    now,
                ),
            )
            self.audit.append(
                action="conversation.transition",
                actor_id="system.conversations",
                actor_type="system",
                resource_type="conversation",
                resource_id=cid,
                new_state="AWAITING_CLARIFICATION",
                trace_id=request_id,
                context_version=context_version,
                details={},
                conn=c,
            )
            return cid

    def close(self, cid: str, state: str, *, selected: list[str] | None = None) -> None:
        with self.db.tx() as c:
            r = c.execute("SELECT state FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            if r is None or r[0] != "AWAITING_CLARIFICATION":
                return
            self._set(
                c, cid, r[0], state, **({"selected_definitions": json.dumps(selected)} if selected else {})
            )

    def too_many_turns(self, conv: Conversation) -> bool:
        return conv.turns + 1 >= self.max_turns
