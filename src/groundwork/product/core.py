"""Product orchestrator: Slack events -> agent requests -> delivery / shadow review -> diagnosis.

Transport-independent (``SlackGateway`` is an interface; ``slack_app.py`` binds it to Bolt), so every
decision below is unit/integration-testable without Slack. Identities come ONLY from the validated
Slack envelope (Bolt verifies request signatures in HTTP mode; Socket Mode is an authenticated
WebSocket). Text inside a message is never treated as identity or authorization.

Idempotency: every inbound event / interaction is recorded in ``slack_events`` by a dedupe key
before any side effect; a retried delivery with the same key is ignored, so a question is never
executed twice and a click never applies twice.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from groundwork.agent.factory import open_agent
from groundwork.agent.providers.base import ModelProvider, ProviderNotConfigured
from groundwork.agent.schemas import AgentConfig, AgentResponse, Outcome
from groundwork.agent.traces import TraceStore
from groundwork.config.settings import ContextMode, Settings
from groundwork.context.loader import ContextStore, load_context
from groundwork.observability.logging import get_logger
from groundwork.product import blocks
from groundwork.product.audit import AuditLog
from groundwork.product.config import ProductConfig
from groundwork.product.conversations import ConversationStore, compose_continuation
from groundwork.product.db import Database
from groundwork.product.deployment import ContextDeployer
from groundwork.product.failures import (
    REVIEWER_CATEGORIES,
    FailureCategory,
    FailureStore,
    classify,
    from_reviewer,
)
from groundwork.product.proposals.store import DecisionRefused, ProposalStore

log = get_logger("product")

CONSENT_TEXT = (
    "*Groundwork pilot: privacy notice*\n"
    "Groundwork answers analytics questions from the governed Olist warehouse. To run the pilot we store, for "
    "{days} days: your Slack user id, your questions, the generated answers and SQL, timings, and any feedback you "
    "give. Query results are not stored beyond a 10-row preview. In *shadow mode* an analyst reviews answers before "
    "you see them. Reports aggregate usage; per-user counts use pseudonymous labels. You can stop at any time by "
    "saying `stop`; ask the pilot owner to delete your data."
)


class SlackGateway(Protocol):
    def post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str: ...
    def update(
        self, channel: str, ts: str, *, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None: ...
    def ephemeral(
        self,
        channel: str,
        user: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> None: ...
    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> None: ...


class SlackDeliveryError(RuntimeError):
    pass


@dataclass
class IncomingMessage:
    team_id: str
    channel_id: str
    user_id: str
    text: str
    ts: str
    thread_ts: str | None
    event_id: str
    channel_type: str  # im | channel | group | mpim
    is_mention: bool


AgentRunner = Callable[[str, str, str, Path, threading.Event], AgentResponse]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class ProductCore:
    def __init__(
        self,
        settings: Settings,
        config: ProductConfig,
        db: Database,
        gateway: SlackGateway,
        *,
        agent_config: AgentConfig,
        provider_factory: Callable[[], ModelProvider] | None = None,
        runner: AgentRunner | None = None,
        on_failure: Callable[[str], None] | None = None,
        workflow: Any | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.settings, self.cfg, self.db = settings, config, db
        self.gw = blocks.ScrubbingGateway(gateway)
        self.audit = AuditLog(db)
        self.failures = FailureStore(db, self.audit)
        self.proposals = ProposalStore(db, self.audit)
        self.convs = ConversationStore(
            db, self.audit, config.conversation_ttl_minutes, config.max_clarification_turns
        )
        self.deployer = ContextDeployer(settings, db, self.audit, alert=self.alert)
        self.agent_config = agent_config
        self.provider_factory = provider_factory
        self.runner = runner or self._default_runner
        self.on_failure = on_failure
        self.workflow = workflow
        self.pool = executor or ThreadPoolExecutor(
            max_workers=config.max_concurrent_requests, thread_name_prefix="cf-req"
        )
        self.cancels: dict[str, threading.Event] = {}
        self.traces = TraceStore(settings.data_dir / "ops" / "agent_traces.sqlite")
        self._store_cache: tuple[Path, ContextStore] | None = None

    # ---------------- helpers ----------------

    def alert(self, text: str) -> None:
        log.warning("alert", extra={"alert": text})
        ch = self.cfg.slack.notify_channel_id
        if ch:
            try:
                self.gw.post(ch, text=f":rotating_light: {text}")
            except Exception:
                log.exception("alert delivery failed")

    def context_dir(self) -> Path:
        return self.deployer.context_dir_for_requests()[0]

    def store(self) -> ContextStore:
        d = self.context_dir()
        if self._store_cache is None or self._store_cache[0] != d:
            self._store_cache = (d, load_context(d))
        return self._store_cache[1]

    def dedupe(self, key: str, kind: str) -> bool:
        """True if this key is new (and is now recorded)."""
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM slack_events WHERE dedupe_key = ?", (key,)).fetchone():
                return False
            c.execute("INSERT INTO slack_events VALUES (?,?,?)", (key, kind, _now()))
        return True

    def _consented(self, team: str, user: str) -> bool | None:
        r = self.db.one(
            "SELECT decision FROM consents WHERE team_id=? AND user_id=? AND notice_version=?",
            (team, user, self.cfg.consent_notice_version),
        )
        return None if r is None else r[0] == "accepted"

    def _rate_limited(self, user: str) -> bool:
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        r = self.db.one("SELECT count(*) FROM requests WHERE user_id=? AND created_at>=?", (user, since))
        return bool(r and r[0] >= self.cfg.per_user_requests_per_hour)

    def _safe_post(
        self,
        channel: str,
        *,
        text: str,
        blocks_: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        request_id: str | None = None,
    ) -> str | None:
        try:
            return self.gw.post(channel, text=text, blocks=blocks_, thread_ts=thread_ts)
        except Exception as exc:
            self.audit.append(
                action="slack.delivery_failed",
                actor_id="system.slack",
                actor_type="system",
                resource_type="request",
                resource_id=request_id or channel,
                trace_id=request_id,
                details={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            return None

    # ---------------- inbound messages ----------------

    def handle_message(self, m: IncomingMessage) -> str | None:
        if not self.dedupe(f"event:{m.event_id}", "message"):
            return None
        thread = m.thread_ts or m.ts
        if not self.cfg.slack.allowed_team_ids or m.team_id not in self.cfg.slack.allowed_team_ids:
            self.audit.append(
                action="question.rejected",
                actor_id=f"slack:{m.user_id}",
                actor_type="human",
                resource_type="slack_team",
                resource_id=m.team_id,
                slack_event_id=m.event_id,
                details={"reason": "workspace not allowed"},
            )
            return None
        user = self.cfg.user(m.user_id)
        text = " ".join(m.text.split())
        if user is None:
            self.audit.append(
                action="question.rejected",
                actor_id=f"slack:{m.user_id}",
                actor_type="human",
                resource_type="slack_user",
                resource_id=m.user_id,
                slack_event_id=m.event_id,
                details={"reason": "not on pilot allowlist"},
            )
            self._safe_post(
                m.channel_id,
                thread_ts=thread,
                text="Sorry — Groundwork is in a limited pilot and your account isn't on the access list. Ask the pilot owner to add you.",  # noqa: E501 - long literal
            )
            return None
        low = text.lower()
        if low in {"stop", "unsubscribe"}:
            self._record_consent(m.team_id, m.user_id, "declined")
            self._safe_post(
                m.channel_id,
                thread_ts=thread,
                text="You've opted out of the pilot. Say anything to see the notice again.",
            )
            return None
        consent = self._consented(m.team_id, m.user_id)
        if self.cfg.require_consent and consent is not True:
            self._safe_post(
                m.channel_id,
                thread_ts=thread,
                text="Please review the pilot privacy notice first.",
                blocks_=blocks.consent_blocks(
                    self.cfg.consent_notice_version, CONSENT_TEXT.format(days=self.cfg.retention_days)
                ),
            )
            return None
        conv = self.convs.open_for(m.team_id, m.channel_id, thread, m.user_id)
        if low in {"cancel", "never mind", "nevermind"}:
            if conv:
                self.convs.close(conv.conversation_id, "CANCELLED")
            for rid, ev in list(self.cancels.items()):
                r = self.db.one("SELECT user_id, thread_ts FROM requests WHERE request_id=?", (rid,))
                if r and r[0] == m.user_id and r[1] == thread:
                    ev.set()
            self._safe_post(m.channel_id, thread_ts=thread, text="Cancelled.")
            return None
        if not text:
            return None
        if self._rate_limited(m.user_id):
            self._safe_post(
                m.channel_id,
                thread_ts=thread,
                text="You've reached the hourly question limit for the pilot. Please try again later.",
            )
            return None
        question = text
        conversation_id = None
        if conv is not None:
            current = self.store().version
            if conv.context_version and conv.context_version != current:
                self.convs.close(conv.conversation_id, "STALE")
                self._safe_post(
                    m.channel_id,
                    thread_ts=thread,
                    text="The governed definitions changed since I asked for clarification, so I won't continue the old plan. Please ask your question again.",  # noqa: E501 - long literal
                )
                return None
            if self.convs.too_many_turns(conv):
                self.convs.close(conv.conversation_id, "CANCELLED")
                self._safe_post(
                    m.channel_id,
                    thread_ts=thread,
                    text="Let's start over — please restate the full question with the details you've given.",
                )
                return None
            question = compose_continuation(conv.original_question, conv.clarification_question or "", text)
            conversation_id = conv.conversation_id
        rid = uuid.uuid4().hex
        delivery = self.cfg.delivery_mode
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO requests (request_id, team_id, channel_id, user_id, thread_ts, message_ts, slack_event_id, question, "  # noqa: E501 - long literal
                "conversation_id, principal, delivery, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    m.team_id,
                    m.channel_id,
                    m.user_id,
                    thread,
                    m.ts,
                    m.event_id,
                    question,
                    conversation_id,
                    user.principal,
                    delivery,
                    "QUEUED",
                    _now(),
                    _now(),
                ),
            )
            self.audit.append(
                action="question.received",
                actor_id=f"slack:{m.user_id}",
                actor_type="human",
                resource_type="request",
                resource_id=rid,
                new_state="QUEUED",
                slack_event_id=m.event_id,
                trace_id=rid,
                details={
                    "principal": user.principal,
                    "delivery": delivery,
                    "continuation": conversation_id is not None,
                },
                conn=c,
            )
        ts = self._safe_post(
            m.channel_id,
            thread_ts=thread,
            text="Working on it…",
            blocks_=blocks.status_blocks(
                ":hourglass_flowing_sand: Working on it… (looking up governed definitions and running a query)"
            ),
            request_id=rid,
        )
        if ts:
            with self.db.tx() as c:
                c.execute("UPDATE requests SET status_message_ts=? WHERE request_id=?", (ts, rid))
        self.cancels[rid] = threading.Event()
        self.pool.submit(self._run_safely, rid)
        return rid

    def _record_consent(self, team: str, user: str, decision: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO consents VALUES (?,?,?,?,?)",
                (team, user, self.cfg.consent_notice_version, decision, _now()),
            )
            self.audit.append(
                action="consent.recorded",
                actor_id=f"slack:{user}",
                actor_type="human",
                resource_type="consent",
                resource_id=f"{team}:{user}",
                new_state=decision,
                details={"notice": self.cfg.consent_notice_version},
                conn=c,
            )

    # ---------------- execution ----------------

    def _default_runner(
        self, question: str, request_id: str, principal: str, ctx: Path, cancel: threading.Event
    ) -> AgentResponse:
        async def main() -> AgentResponse:
            prov = self.provider_factory() if self.provider_factory else None
            ev = asyncio.Event()
            loop = asyncio.get_running_loop()
            watcher = threading.Thread(
                target=lambda: (cancel.wait(), loop.call_soon_threadsafe(ev.set)), daemon=True
            )
            watcher.start()
            try:
                async with open_agent(
                    self.settings,
                    self.agent_config,
                    mode=ContextMode.full_context,
                    principal=principal,
                    provider=prov,
                    trace_store=self.traces,
                    context_dir=ctx,
                ) as agent:
                    return (await agent.ask(question, request_id=request_id, cancel_event=ev)).response
            finally:
                if prov is not None:
                    await prov.aclose()
                if not cancel.is_set():
                    cancel.set()  # release the watcher thread

        return asyncio.run(main())

    def _run_safely(self, rid: str) -> None:
        try:
            self.run_request(rid)
        except Exception as exc:
            log.exception("request failed", extra={"request_id": rid})
            self._finish_failed(rid, "internal_error", f"{type(exc).__name__}")

    def _finish_failed(self, rid: str, code: str, msg: str, user_text: str | None = None) -> None:
        r = self.db.one("SELECT * FROM requests WHERE request_id=?", (rid,))
        if r is None:
            return
        with self.db.tx() as c:
            c.execute(
                "UPDATE requests SET status='FAILED', error_code=?, updated_at=? WHERE request_id=?",
                (code, _now(), rid),
            )
        text = user_text or "Something went wrong and no answer was produced. The team has been notified."
        self._deliver_text(dict(r), f":x: {text} (trace `{rid}`)")
        self.cancels.pop(rid, None)

    def _deliver_text(self, r: dict[str, Any], text: str) -> None:
        try:
            if r.get("status_message_ts"):
                self.gw.update(
                    r["channel_id"], r["status_message_ts"], text=text, blocks=blocks.status_blocks(text)
                )
            else:
                self.gw.post(r["channel_id"], text=text, thread_ts=r["thread_ts"])
        except Exception as exc:
            self.audit.append(
                action="slack.delivery_failed",
                actor_id="system.slack",
                actor_type="system",
                resource_type="request",
                resource_id=r["request_id"],
                trace_id=r["request_id"],
                details={"error": type(exc).__name__},
            )

    def run_request(self, rid: str) -> AgentResponse | None:
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM requests WHERE request_id=?", (rid,)).fetchone()
            if r is None or r["status"] != "QUEUED":
                return None
            c.execute("UPDATE requests SET status='RUNNING', updated_at=? WHERE request_id=?", (_now(), rid))
        r = dict(r)
        cancel = self.cancels.setdefault(rid, threading.Event())
        ctx = self.context_dir()
        store = self.store()
        try:
            resp = self.runner(r["question"], rid, r["principal"], ctx, cancel)
        except ProviderNotConfigured as exc:
            self.audit.append(
                action="agent.executed",
                actor_id="system.agent",
                actor_type="system",
                resource_type="request",
                resource_id=rid,
                new_state="BLOCKED",
                trace_id=rid,
                details={"missing": exc.missing},
            )
            self._finish_failed(
                rid,
                "provider_not_configured",
                exc.message,
                "The AI model isn't configured on this deployment, so no answer was produced (BLOCKED).",
            )
            return None
        failure = classify(resp, store)
        status = "CANCELLED" if resp.error and resp.error.code == "cancelled" else "DONE"
        with self.db.tx() as c:
            c.execute(
                "UPDATE requests SET status=?, outcome=?, error_code=?, failure_category=?, context_version=?, model=?, latency_ms=?, "  # noqa: E501 - long literal
                "input_tokens=?, output_tokens=?, cost_usd=?, response_json=?, updated_at=? WHERE request_id=?",
                (
                    status,
                    resp.outcome.value,
                    resp.error.code if resp.error else None,
                    failure.category.value if failure else None,
                    resp.context_version or store.version,
                    ",".join(resp.model.resolved_models[:1]) or resp.model.requested_model,
                    resp.latency_ms,
                    resp.usage.input_tokens
                    + resp.usage.cache_read_input_tokens
                    + resp.usage.cache_creation_input_tokens,
                    resp.usage.output_tokens,
                    resp.cost.estimated_usd,
                    resp.model_dump_json(),
                    _now(),
                    rid,
                ),
            )
            self.audit.append(
                action="agent.executed",
                actor_id="system.agent",
                actor_type="system",
                resource_type="request",
                resource_id=rid,
                prior_state="RUNNING",
                new_state=resp.outcome.value,
                trace_id=rid,
                context_version=resp.context_version,
                details={
                    "queries": len(resp.queries),
                    "error": resp.error.code if resp.error else None,
                    "model": resp.model.requested_model,
                    "live": resp.model.live,
                },
                conn=c,
            )
            if resp.outcome == Outcome.POLICY_DENIED:
                self.audit.append(
                    action="policy.decision",
                    actor_id="system.policy",
                    actor_type="system",
                    resource_type="request",
                    resource_id=rid,
                    new_state="DENIED",
                    trace_id=rid,
                    details={},
                    conn=c,
                )
        self.cancels.pop(rid, None)
        if failure is not None:
            self.failures.record(failure)
            if failure.context_fix_appropriate and self.on_failure is not None:
                try:
                    self.on_failure(failure.failure_id)
                except Exception:
                    log.exception("improvement trigger failed")
        if status == "CANCELLED":
            self._deliver_text(r, "Cancelled.")
            return resp
        # conversation state
        if resp.outcome == Outcome.CLARIFICATION_REQUIRED and resp.clarification_question:
            conv = self.convs.open_for(r["team_id"], r["channel_id"], r["thread_ts"], r["user_id"])
            original = conv.original_question if conv else r["question"]
            self.convs.await_clarification(
                team=r["team_id"],
                channel=r["channel_id"],
                thread=r["thread_ts"],
                user=r["user_id"],
                original=original,
                question=resp.clarification_question,
                context_version=resp.context_version or store.version,
                request_id=rid,
                existing=conv,
            )
        elif r["conversation_id"]:
            self.convs.close(
                r["conversation_id"], "RESOLVED", selected=resp.referenced_metrics + resp.referenced_terms
            )
        self.deliver(rid, resp, store)
        return resp

    # ---------------- delivery ----------------

    def deliver(self, rid: str, resp: AgentResponse, store: ContextStore | None) -> None:
        r = dict(self.db.one("SELECT * FROM requests WHERE request_id=?", (rid,)) or {})
        if r.get("delivered_at"):
            return
        if r["delivery"] == "shadow" and resp.outcome == Outcome.ANSWER:
            self._hold_for_review(r, resp, store)
            return
        bl = blocks.answer_blocks(resp, store, request_id=rid)
        self._post_answer(r, bl, resp.answer[:300] or resp.outcome.value)

    def _post_answer(self, r: dict[str, Any], bl: list[dict[str, Any]], fallback: str) -> None:
        try:
            if r.get("status_message_ts"):
                self.gw.update(r["channel_id"], r["status_message_ts"], text=fallback, blocks=bl)
            else:
                self.gw.post(r["channel_id"], text=fallback, blocks=bl, thread_ts=r["thread_ts"])
        except Exception as exc:
            self.audit.append(
                action="slack.delivery_failed",
                actor_id="system.slack",
                actor_type="system",
                resource_type="request",
                resource_id=r["request_id"],
                trace_id=r["request_id"],
                details={"error": type(exc).__name__},
            )
            return
        with self.db.tx() as c:
            c.execute(
                "UPDATE requests SET delivered_at=? WHERE request_id=? AND delivered_at IS NULL",
                (_now(), r["request_id"]),
            )
            self.audit.append(
                action="answer.delivered",
                actor_id="system.slack",
                actor_type="system",
                resource_type="request",
                resource_id=r["request_id"],
                trace_id=r["request_id"],
                details={"delivery": r["delivery"]},
                conn=c,
            )

    def _hold_for_review(self, r: dict[str, Any], resp: AgentResponse, store: ContextStore | None) -> None:
        with self.db.tx() as c:
            existing = c.execute(
                "SELECT review_id, review_message_ts FROM answer_reviews WHERE request_id=?",
                (r["request_id"],),
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO answer_reviews (review_id, request_id, state, created_at) VALUES (?,?,?,?)",
                    (f"rev-{uuid.uuid4().hex[:10]}", r["request_id"], "PENDING", _now()),
                )
                self.audit.append(
                    action="answer.review_requested",
                    actor_id="system.shadow",
                    actor_type="system",
                    resource_type="request",
                    resource_id=r["request_id"],
                    new_state="PENDING",
                    trace_id=r["request_id"],
                    details={},
                    conn=c,
                )
        if existing is not None and existing[1]:
            return
        self._deliver_text(
            r,
            ":mag: Thanks — an analyst is reviewing the answer before it's sent (pilot shadow mode). You'll get it in this thread.",  # noqa: E501 - long literal
        )
        ch = self.cfg.slack.review_channel_id
        if not ch:
            self.alert(f"Shadow review pending for {r['request_id']} but no review_channel_id is configured")
            return
        ts = self._safe_post(
            ch,
            text=f"Answer review needed: {resp.question[:150]}",
            blocks_=blocks.shadow_review_blocks(r["request_id"], r["user_id"], resp, store),
            request_id=r["request_id"],
        )
        if ts:
            with self.db.tx() as c:
                c.execute(
                    "UPDATE answer_reviews SET review_message_ts=? WHERE request_id=?", (ts, r["request_id"])
                )

    def response_for(self, rid: str) -> tuple[dict[str, Any], AgentResponse] | None:
        r = self.db.one("SELECT * FROM requests WHERE request_id=?", (rid,))
        if r is None or not r["response_json"]:
            return None
        return dict(r), AgentResponse.model_validate_json(r["response_json"])

    # ---------------- interactions ----------------

    def handle_action(
        self,
        *,
        action_id: str,
        value: str,
        user_id: str,
        team_id: str,
        channel_id: str,
        trigger_id: str | None,
        dedupe_key: str,
        message_ts: str | None = None,
    ) -> None:
        if team_id not in self.cfg.slack.allowed_team_ids:
            return
        if not self.dedupe(f"action:{dedupe_key}", action_id):
            return
        if action_id in ("cf_consent_accept", "cf_consent_decline"):
            if value != self.cfg.consent_notice_version:
                self.gw.ephemeral(
                    channel_id,
                    user_id,
                    text="That notice is out of date; send a new message to see the current one.",
                )
                return
            accepted = action_id == "cf_consent_accept"
            self._record_consent(team_id, user_id, "accepted" if accepted else "declined")
            self.gw.ephemeral(
                channel_id,
                user_id,
                text="Thanks — you can ask your question now."
                if accepted
                else "OK, nothing will be recorded.",
            )
            return
        if action_id == "cf_show_details":
            got = self.response_for(value)
            if got is None:
                return
            r, resp = got
            if (
                user_id != r["user_id"]
                and not self.cfg.answer_reviewer(user_id)
                and not self.cfg.context_reviewer(user_id)
            ):
                self.gw.ephemeral(
                    channel_id,
                    user_id,
                    text="Only the person who asked (or a reviewer) can open these details.",
                )
                return
            if (
                r["delivery"] == "shadow"
                and resp.outcome == Outcome.ANSWER
                and user_id == r["user_id"]
                and not self._approved(value)
            ):
                self.gw.ephemeral(
                    channel_id, user_id, text="Details become available once the answer is approved."
                )
                return
            self.gw.ephemeral(
                channel_id,
                user_id,
                text="SQL and context details",
                blocks=blocks.details_blocks(resp, self.store()),
                thread_ts=r["thread_ts"],
            )
            return
        if action_id in ("cf_feedback_up", "cf_feedback_down"):
            fr = self.db.one("SELECT user_id FROM requests WHERE request_id=?", (value,))
            if fr is None or fr[0] != user_id:
                self.gw.ephemeral(
                    channel_id, user_id, text="Feedback is collected from the person who asked."
                )
                return
            if action_id == "cf_feedback_down" and trigger_id:
                self.gw.open_modal(
                    trigger_id,
                    blocks.reason_modal(
                        "cf_feedback_modal",
                        "What was wrong?",
                        value,
                        label="Tell us what was wrong (optional details)",
                        with_category=[
                            "wrong_number",
                            "wrong_definition",
                            "missing_data",
                            "too_slow",
                            "other",
                        ],
                    ),
                )
                return
            self.record_feedback(
                value, user_id, "up" if action_id == "cf_feedback_up" else "down", None, None
            )
            self.gw.ephemeral(channel_id, user_id, text="Thanks for the feedback!")
            return
        if action_id in ("cf_answer_approve", "cf_answer_reject", "cf_answer_clarify"):
            self._shadow_action(action_id, value, user_id, channel_id, trigger_id, dedupe_key)
            return
        if action_id.startswith("cf_prop_"):
            self._proposal_action(action_id, value, user_id, channel_id, trigger_id, dedupe_key)

    def _approved(self, rid: str) -> bool:
        r = self.db.one("SELECT state FROM answer_reviews WHERE request_id=?", (rid,))
        return bool(r and r[0] == "APPROVED")

    def record_feedback(
        self, rid: str, user_id: str, rating: str, category: str | None, comment: str | None
    ) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO feedback VALUES (?,?,?,?,?,?,?)",
                (
                    f"fb-{uuid.uuid4().hex[:10]}",
                    rid,
                    user_id,
                    rating,
                    category,
                    (comment or "")[:2000] or None,
                    _now(),
                ),
            )
            self.audit.append(
                action="feedback.recorded",
                actor_id=f"slack:{user_id}",
                actor_type="human",
                resource_type="request",
                resource_id=rid,
                new_state=rating,
                trace_id=rid,
                details={"category": category},
                conn=c,
            )

    # ---- shadow-mode answer review (separate from context-change approval) ----

    def _shadow_action(
        self, action_id: str, rid: str, user_id: str, channel_id: str, trigger_id: str | None, key: str
    ) -> None:
        reviewer = self.cfg.answer_reviewer(user_id)
        got = self.response_for(rid)
        if reviewer is None or got is None:
            self.audit.append(
                action="approval.denied_unauthorized",
                actor_id=f"slack:{user_id}",
                actor_type="human",
                resource_type="answer_review",
                resource_id=rid,
                details={"action": action_id},
            )
            self.gw.ephemeral(channel_id, user_id, text="You are not an authorized answer reviewer.")
            return
        r, _ = got
        if r["user_id"] == user_id:
            self.gw.ephemeral(channel_id, user_id, text="You can't review the answer to your own question.")
            return
        if action_id == "cf_answer_approve":
            self.decide_answer(rid, user_id, "APPROVED", note=None, correction=None, category=None, key=key)
        elif trigger_id:
            if action_id == "cf_answer_reject":
                view = blocks.reason_modal(
                    "cf_answer_reject_modal",
                    "Correct / reject",
                    rid,
                    label="Why is it wrong? (kept in the audit log)",
                    with_category=[c.value for c in REVIEWER_CATEGORIES],
                    with_correction=True,
                )
            else:
                view = blocks.reason_modal(
                    "cf_answer_clarify_modal", "Ask the user", rid, label="Question to send to the user"
                )
            self.gw.open_modal(trigger_id, view)

    def decide_answer(
        self,
        rid: str,
        reviewer_id: str,
        state: str,
        *,
        note: str | None,
        correction: str | None,
        category: str | None,
        key: str,
    ) -> bool:
        got = self.response_for(rid)
        if got is None or self.cfg.answer_reviewer(reviewer_id) is None:
            return False
        r, resp = got
        reviewer = self.cfg.answer_reviewer(reviewer_id)
        if r["user_id"] == reviewer_id or reviewer is None:
            return False
        with self.db.tx() as c:
            cur = c.execute(
                "SELECT state, review_message_ts FROM answer_reviews WHERE request_id=?", (rid,)
            ).fetchone()
            if cur is None or cur[0] != "PENDING":
                self.audit.append(
                    action="approval.denied_stale",
                    actor_id=f"slack:{reviewer_id}",
                    actor_type="human",
                    resource_type="answer_review",
                    resource_id=rid,
                    details={"state": cur[0] if cur else None},
                    conn=c,
                )
                return False
            c.execute(
                "UPDATE answer_reviews SET state=?, reviewer_user_id=?, reviewer_note=?, decided_at=? WHERE request_id=?",
                (state, reviewer_id, note, _now(), rid),
            )
            action = {
                "APPROVED": "answer.approved",
                "REJECTED": "answer.rejected",
                "CORRECTED": "answer.corrected",
                "CLARIFICATION_REQUESTED": "answer.clarification_requested",
            }[state]
            self.audit.append(
                action=action,
                actor_id=reviewer.owner_id,
                actor_type="human",
                resource_type="answer_review",
                resource_id=rid,
                prior_state="PENDING",
                new_state=state,
                trace_id=rid,
                slack_event_id=key,
                details={"category": category, "note": (note or "")[:500], "slack_user_id": reviewer_id},
                conn=c,
            )
        review_ts = cur[1]
        store = self.store()
        if state == "APPROVED":
            self._post_answer(
                r,
                blocks.answer_blocks(resp, store, request_id=rid, reviewed_by=reviewer_id),
                resp.answer[:300],
            )
        elif state == "CLARIFICATION_REQUESTED":
            q = note or ""
            self._safe_post(
                r["channel_id"],
                thread_ts=r["thread_ts"],
                request_id=rid,
                text=f"<@{reviewer_id}> (analyst) asks: {blocks.esc(q)}\n_Reply in this thread to continue._",
            )
            self.convs.await_clarification(
                team=r["team_id"],
                channel=r["channel_id"],
                thread=r["thread_ts"],
                user=r["user_id"],
                original=r["question"],
                question=q,
                context_version=resp.context_version or store.version,
                request_id=rid,
                existing=None,
            )
        else:
            msg = (
                f"An analyst (<@{reviewer_id}>) reviewed the answer and wrote:\n>{blocks.esc(correction)}"
                if correction
                else f"An analyst (<@{reviewer_id}>) reviewed the generated answer and could not confirm it, so it was not sent. They may follow up."  # noqa: E501 - long literal
            )
            self._safe_post(r["channel_id"], thread_ts=r["thread_ts"], text=msg, request_id=rid)
            if category:
                rec = from_reviewer(resp, FailureCategory(category), reviewer_id, note or "")
                rec.trace_id = rid
                self.failures.record(rec, actor=f"human:{reviewer_id}")
                if rec.context_fix_appropriate and self.on_failure:
                    try:
                        self.on_failure(rec.failure_id)
                    except Exception:
                        log.exception("improvement trigger failed")
        if review_ts and self.cfg.slack.review_channel_id:
            try:
                self.gw.update(
                    self.cfg.slack.review_channel_id,
                    review_ts,
                    text=f"Reviewed: {state}",
                    blocks=blocks.status_blocks(f"Answer review {rid[:8]}: {state} by <@{reviewer_id}>"),
                )
            except Exception:
                log.exception("review card update failed")
        return True

    # ---- context-change approval ----

    def _proposal_action(
        self, action_id: str, value: str, user_id: str, channel_id: str, trigger_id: str | None, key: str
    ) -> None:
        try:
            v = json.loads(value)
            pid, rev = str(v["pid"]), int(v["rev"])
        except (ValueError, KeyError, TypeError):
            return
        row = self.proposals.get(pid)
        if row is None:
            return
        if action_id == "cf_prop_diff":
            if self.cfg.context_reviewer(user_id) is None:
                self.gw.ephemeral(
                    channel_id, user_id, text="Only context reviewers can open proposal diffs here."
                )
                return
            self.gw.ephemeral(channel_id, user_id, text=f"Diff for {pid}", blocks=blocks.diff_blocks(row))
            return
        if action_id == "cf_prop_approve":
            self.decide_proposal(pid, rev, user_id, "approve", None, key, channel_id)
            return
        if trigger_id:
            cb = "cf_prop_reject_modal" if action_id == "cf_prop_reject" else "cf_prop_changes_modal"
            self.gw.open_modal(
                trigger_id,
                blocks.reason_modal(
                    cb,
                    "Reject" if cb.endswith("reject_modal") else "Request changes",
                    json.dumps({"pid": pid, "rev": rev, "ch": channel_id}),
                ),
            )

    def decide_proposal(
        self,
        pid: str,
        rev: int,
        user_id: str,
        decision: Literal["approve", "reject", "request_changes"],
        reason: str | None,
        key: str,
        channel_id: str,
    ) -> bool:
        try:
            st = self.proposals.decide(
                pid,
                decision=decision,
                slack_user_id=user_id,
                revision=rev,
                config=self.cfg,  # type: ignore[arg-type]
                reason=reason,
                slack_event_id=key,
            )
        except DecisionRefused as exc:
            self.gw.ephemeral(channel_id, user_id, text=f"Not recorded: {exc.reason}.")
            return False
        row = self.proposals.get(pid)
        if row and row.raw.get("review_message_ts") and self.cfg.slack.review_channel_id:
            try:
                self.gw.update(
                    self.cfg.slack.review_channel_id,
                    row.raw["review_message_ts"],
                    text=f"{pid}: {st.value}",
                    blocks=blocks.proposal_review_blocks(row)[:2]
                    + blocks.status_blocks(f"Decision: {st.value} by <@{user_id}> (revision {rev})"),
                )
            except Exception:
                log.exception("proposal card update failed")
        if self.workflow is not None and row and row.raw.get("pr_number"):
            try:
                gh = self.workflow.github_factory()
                gh.comment(
                    int(row.raw["pr_number"]),
                    f"Groundwork reviewer decision for revision {rev}: **{st.value}**"
                    + (f"\n\n> {reason}" if reason else ""),
                )
            except Exception:
                log.exception("PR comment failed")
        return True

    def handle_view_submission(
        self,
        *,
        callback_id: str,
        private_metadata: str,
        values: dict[str, Any],
        user_id: str,
        team_id: str,
        dedupe_key: str,
    ) -> None:
        if team_id not in self.cfg.slack.allowed_team_ids or not self.dedupe(
            f"view:{dedupe_key}", callback_id
        ):
            return

        def val(block: str) -> str | None:
            v = (values.get(block) or {}).get("v") or {}
            return v.get("value") or (v.get("selected_option") or {}).get("value")

        if callback_id == "cf_feedback_modal":
            r = self.db.one("SELECT user_id FROM requests WHERE request_id=?", (private_metadata,))
            if r and r[0] == user_id:
                self.record_feedback(private_metadata, user_id, "down", val("category"), val("reason"))
        elif callback_id == "cf_answer_reject_modal":
            corr = val("correction")
            self.decide_answer(
                private_metadata,
                user_id,
                "CORRECTED" if corr else "REJECTED",
                note=val("reason"),
                correction=corr,
                category=val("category"),
                key=dedupe_key,
            )
        elif callback_id == "cf_answer_clarify_modal":
            self.decide_answer(
                private_metadata,
                user_id,
                "CLARIFICATION_REQUESTED",
                note=val("reason"),
                correction=None,
                category=None,
                key=dedupe_key,
            )
        elif callback_id in ("cf_prop_reject_modal", "cf_prop_changes_modal"):
            meta = json.loads(private_metadata)
            self.decide_proposal(
                meta["pid"],
                int(meta["rev"]),
                user_id,
                "reject" if callback_id == "cf_prop_reject_modal" else "request_changes",
                val("reason"),
                dedupe_key,
                meta.get("ch") or (self.cfg.slack.review_channel_id or ""),
            )

    # ---------------- edits / deletions ----------------

    def handle_message_changed(self, team: str, channel: str, message_ts: str, deleted: bool) -> None:
        r = self.db.one(
            "SELECT * FROM requests WHERE channel_id=? AND message_ts=? ORDER BY created_at DESC LIMIT 1",
            (channel, message_ts),
        )
        if r is None:
            return
        if r["status"] in ("QUEUED", "RUNNING") and deleted:
            ev = self.cancels.get(r["request_id"])
            if ev:
                ev.set()
        conv = self.convs.open_for(team, channel, r["thread_ts"], r["user_id"])
        if conv:
            self.convs.close(conv.conversation_id, "STALE")
        if not deleted:
            self._safe_post(
                channel,
                thread_ts=r["thread_ts"],
                text="I answered the original wording; edits aren't re-processed. Ask again if the question changed.",
            )

    # ---------------- recovery ----------------

    def recover_on_startup(self) -> int:
        """Requests that were QUEUED/RUNNING when the process stopped are NOT re-executed silently:
        they are marked INTERRUPTED and the user is told to ask again."""
        rows = self.db.query("SELECT * FROM requests WHERE status IN ('QUEUED','RUNNING')")
        for r in rows:
            with self.db.tx() as c:
                c.execute(
                    "UPDATE requests SET status='INTERRUPTED', updated_at=? WHERE request_id=?",
                    (_now(), r["request_id"]),
                )
                self.audit.append(
                    action="request.interrupted",
                    actor_id="system.recovery",
                    actor_type="system",
                    resource_type="request",
                    resource_id=r["request_id"],
                    prior_state=r["status"],
                    new_state="INTERRUPTED",
                    trace_id=r["request_id"],
                    details={},
                    conn=c,
                )
            self._deliver_text(
                dict(r),
                ":warning: Groundwork restarted before this finished, so no answer was produced. Please ask again.",
            )
        return len(rows)

    def redeliver_pending(self) -> int:
        """Retry answers that finished but whose Slack delivery failed (idempotent via delivered_at)."""
        n = 0
        for r in self.db.query(
            "SELECT request_id FROM requests WHERE status='DONE' AND delivered_at IS NULL AND delivery='live'"
        ):
            got = self.response_for(r[0])
            if got:
                self.deliver(r[0], got[1], self.store())
                n += 1
        return n
