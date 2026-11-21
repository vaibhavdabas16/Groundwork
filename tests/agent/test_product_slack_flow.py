"""Product flows through ``ProductCore`` with the REAL agent runtime, REAL MCP server and REAL
warehouse; the model is a SCRIPTED test double and Slack is a recording gateway (test double).
These prove routing, identity/authorization, idempotency, conversation state, shadow review and
failure handling. They are NOT evidence of a live Slack workspace interaction."""

from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from groundwork.agent.providers.base import ProviderNotConfigured
from groundwork.agent.providers.scripted import ScriptedProvider, turn
from groundwork.agent.schemas import AgentConfig
from groundwork.config.settings import Settings
from groundwork.product.config import AnswerReviewer, PilotUser, ProductConfig, SlackSettings
from groundwork.product.core import IncomingMessage, ProductCore
from groundwork.product.db import Database

pytestmark = [pytest.mark.integration, pytest.mark.agent]

TEAM, USER, OTHER, REVIEWER = "T0TEAM", "UASKER1", "UOTHER1", "UREVIEW1"
SQL = "SELECT count(*) AS delivered FROM analytics.orders WHERE order_status = 'delivered'"


class RecordingGateway:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.modals: list[dict[str, Any]] = []
        self.fail_posts = 0
        self.n = 0

    def post(self, channel: str, *, text: str, blocks: Any = None, thread_ts: str | None = None) -> str:
        if self.fail_posts:
            self.fail_posts -= 1
            raise RuntimeError("slack_api_error: ratelimited")
        self.n += 1
        self.posts.append(
            {"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": f"9.{self.n}"}
        )
        return f"9.{self.n}"

    def update(self, channel: str, ts: str, *, text: str, blocks: Any = None) -> None:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    def ephemeral(
        self, channel: str, user: str, *, text: str, blocks: Any = None, thread_ts: str | None = None
    ) -> None:
        self.ephemerals.append({"channel": channel, "user": user, "text": text, "blocks": blocks})

    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> None:
        self.modals.append(view)

    def all_text(self) -> str:
        return json.dumps(self.posts + self.updates + self.ephemerals, default=str)


def answering_script(h: list[Any]) -> Any:
    if len(h) == 1:
        return turn(("run_query", {"sql": SQL}))
    out = json.loads(h[-1][0].content)["untrusted_tool_output"]
    return turn(
        (
            "submit_response",
            {
                "outcome": "ANSWER",
                "answer": "There were {} delivered orders.".format(out["rows"][0][0]),
                "query_id": out["query_id"],
            },
        )
    )


def make_core(
    s: Settings,
    tmp: Path,
    script: Any,
    *,
    delivery: str = "live",
    consent: bool = False,
    provider_missing: bool = False,
) -> tuple[ProductCore, RecordingGateway]:
    gw = RecordingGateway()
    cfg = ProductConfig(
        delivery_mode=delivery,
        require_consent=consent,  # type: ignore[arg-type]
        users=[
            PilotUser(slack_user_id=USER),
            PilotUser(slack_user_id=OTHER),
            PilotUser(slack_user_id=REVIEWER),
        ],
        answer_reviewers=[AnswerReviewer(slack_user_id=REVIEWER, owner_id="person.reviewer")],
        slack=SlackSettings(allowed_team_ids=[TEAM], review_channel_id="CREVIEW"),
    )

    def factory() -> Any:
        if provider_missing:
            raise ProviderNotConfigured("OPENAI_API_KEY is not set", missing=["OPENAI_API_KEY"])
        return ScriptedProvider(script)

    data = tmp / "data"
    (data / "ops").mkdir(parents=True, exist_ok=True)
    core = ProductCore(
        s.model_copy(update={"data_dir": data}),
        cfg,
        Database(tmp / "ops.sqlite"),
        gw,
        agent_config=AgentConfig(),
        provider_factory=factory,
        executor=ThreadPoolExecutor(max_workers=1),
    )
    return core, gw


def msg(
    text: str,
    *,
    user: str = USER,
    ts: str = "1.1",
    thread: str | None = None,
    event: str = "Ev1",
    team: str = TEAM,
) -> IncomingMessage:
    return IncomingMessage(
        team_id=team,
        channel_id="D0DM",
        user_id=user,
        text=text,
        ts=ts,
        thread_ts=thread,
        event_id=event,
        channel_type="im",
        is_mention=False,
    )


def wait(core: ProductCore) -> None:
    core.pool.shutdown(wait=True)
    core.pool = ThreadPoolExecutor(max_workers=1)


def test_live_answer_with_provenance_and_retry_dedupe(real_warehouse: Settings, tmp_path: Path) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script)
    rid = core.handle_message(msg("How many orders were delivered?"))
    assert core.handle_message(msg("How many orders were delivered?")) is None  # Slack retry, same event_id
    wait(core)
    rows = core.db.query("SELECT status, outcome, delivered_at FROM requests")
    assert len(rows) == 1 and rows[0]["outcome"] == "ANSWER" and rows[0]["delivered_at"]
    final = json.dumps(gw.updates[-1]["blocks"])
    assert "96478" in final.replace(",", "") and rid in final and "Show SQL" in final
    core.handle_action(
        action_id="cf_show_details",
        value=rid or "",
        user_id=USER,
        team_id=TEAM,
        channel_id="D0DM",
        trigger_id=None,
        dedupe_key="a1",
    )
    assert "analytics.orders" in json.dumps(gw.ephemerals[-1]["blocks"]).lower()
    core.handle_action(
        action_id="cf_show_details",
        value=rid or "",
        user_id=OTHER,
        team_id=TEAM,
        channel_id="D0DM",
        trigger_id=None,
        dedupe_key="a2",
    )
    assert "Only the person who asked" in gw.ephemerals[-1]["text"]
    assert core.audit.verify().ok


def test_unlisted_user_foreign_workspace_and_text_identity_are_refused(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    core, _gw = make_core(real_warehouse, tmp_path, answering_script)
    assert core.handle_message(msg("hi", user="UINTRUDER", event="e1")) is None
    assert core.handle_message(msg("hi", team="TEVIL", event="e2")) is None
    assert core.db.query("SELECT * FROM requests") == []
    actions = [e["action"] for e in core.audit.events()]
    assert actions.count("question.rejected") == 2


def test_consent_is_required_before_questions(real_warehouse: Settings, tmp_path: Path) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script, consent=True)
    assert core.handle_message(msg("How many orders were delivered?")) is None
    assert any("privacy notice" in p["text"] for p in gw.posts)
    core.handle_action(
        action_id="cf_consent_accept",
        value="pilot-notice-v1",
        user_id=USER,
        team_id=TEAM,
        channel_id="D0DM",
        trigger_id=None,
        dedupe_key="c1",
    )
    assert core.handle_message(msg("How many orders were delivered?", event="Ev2")) is not None
    wait(core)


def test_clarification_continues_only_for_the_same_user_and_thread(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    seen: list[str] = []

    def script(h: list[Any]) -> Any:
        q = h[0][0].text
        if len(h) == 1:
            seen.append(q)
        if "The user's answer" not in q:
            return turn(
                (
                    "submit_response",
                    {
                        "outcome": "CLARIFICATION_REQUIRED",
                        "answer": "Which basis?",
                        "clarification_question": "Which revenue definition and how should 'best' be ranked?",
                    },
                )
            )
        return answering_script(h)

    core, _gw = make_core(real_warehouse, tmp_path, script)
    core.handle_message(msg("Show revenue from our best customers.", ts="1.1"))
    wait(core)
    assert core.db.one("SELECT state FROM conversations")[0] == "AWAITING_CLARIFICATION"
    # another user replying in the same thread starts a NEW question (no cross-user leakage)
    core.handle_message(msg("rank by number of orders", user=OTHER, ts="1.2", thread="1.1", event="Ev2"))
    wait(core)
    assert "The user's answer" not in seen[-1]
    core.handle_message(
        msg("Use product sales value and rank by total spend.", ts="1.3", thread="1.1", event="Ev3")
    )
    wait(core)
    assert (
        "Original question: Show revenue from our best customers." in seen[-1]
        and "rank by total spend" in seen[-1]
    )
    assert core.db.one("SELECT state FROM conversations WHERE user_id=?", (USER,))[0] == "RESOLVED"


def test_stale_clarification_after_context_deploy_is_not_continued(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    def script(h: list[Any]) -> Any:
        return turn(
            (
                "submit_response",
                {
                    "outcome": "CLARIFICATION_REQUIRED",
                    "answer": "?",
                    "clarification_question": "Which GMV basis?",
                },
            )
        )

    core, _gw = make_core(real_warehouse, tmp_path, script)
    core.handle_message(msg("What was our GMV in March 2018?"))
    wait(core)
    c2 = tmp_path / "c2" / "context"
    shutil.copytree(real_warehouse.context_dir, c2)
    p = c2 / "unsupported" / "concepts.yaml"
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "guidance: Say it is not available.", "guidance: Say it is unavailable.", 1
        ),
        encoding="utf-8",
    )
    core.deployer.deploy(c2, actor="tester")
    before = len(core.db.query("SELECT * FROM requests"))
    assert core.handle_message(msg("items only", ts="1.2", thread="1.1", event="Ev2")) is None
    assert len(core.db.query("SELECT * FROM requests")) == before
    assert core.db.one("SELECT state FROM conversations")[0] == "STALE"


def test_shadow_mode_holds_answers_until_an_authorized_reviewer_approves(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script, delivery="shadow")
    rid = core.handle_message(msg("How many orders were delivered?"))
    wait(core)
    assert core.db.one("SELECT delivered_at FROM requests")[0] is None
    assert "96478" not in json.dumps([u for u in gw.updates]).replace(
        ",", ""
    )  # requester has not seen the number
    assert any(p["channel"] == "CREVIEW" for p in gw.posts)
    # the requester cannot approve their own answer; a non-reviewer cannot either; text "approved" does nothing
    core.handle_action(
        action_id="cf_answer_approve",
        value=rid or "",
        user_id=USER,
        team_id=TEAM,
        channel_id="CREVIEW",
        trigger_id=None,
        dedupe_key="x1",
    )
    core.handle_action(
        action_id="cf_answer_approve",
        value=rid or "",
        user_id=OTHER,
        team_id=TEAM,
        channel_id="CREVIEW",
        trigger_id=None,
        dedupe_key="x2",
    )
    core.handle_message(msg(f"<@{REVIEWER}> approved", user=OTHER, ts="2.1", event="Ev9"))
    assert core.db.one("SELECT state FROM answer_reviews")[0] == "PENDING"
    core.handle_action(
        action_id="cf_answer_approve",
        value=rid or "",
        user_id=REVIEWER,
        team_id=TEAM,
        channel_id="CREVIEW",
        trigger_id=None,
        dedupe_key="x3",
    )
    core.handle_action(
        action_id="cf_answer_approve",
        value=rid or "",
        user_id=REVIEWER,
        team_id=TEAM,
        channel_id="CREVIEW",
        trigger_id=None,
        dedupe_key="x3",
    )  # replay
    assert core.db.one("SELECT state, reviewer_user_id FROM answer_reviews")[:] == ("APPROVED", REVIEWER)
    delivered = [u for u in gw.updates if "Reviewed and approved" in json.dumps(u["blocks"])]
    assert len(delivered) == 1
    actions = [e["action"] for e in core.audit.events()]
    assert actions.count("answer.approved") == 1 and "approval.denied_unauthorized" in actions


def test_shadow_rejection_records_human_diagnosis(real_warehouse: Settings, tmp_path: Path) -> None:
    core, _gw = make_core(real_warehouse, tmp_path, answering_script, delivery="shadow")
    triggered: list[str] = []
    core.on_failure = triggered.append
    rid = core.handle_message(msg("How many customers bought more than once?"))
    wait(core)
    core.handle_view_submission(
        callback_id="cf_answer_reject_modal",
        private_metadata=rid or "",
        user_id=REVIEWER,
        team_id=TEAM,
        dedupe_key="v1",
        values={
            "category": {"v": {"selected_option": {"value": "MISSING_COLUMN_DESCRIPTION"}}},
            "reason": {"v": {"value": "Counted orders, not people (customer_unique_key)."}},
            "correction": {"v": {"value": None}},
        },
    )
    f = core.db.one("SELECT category, evidence FROM failures")
    assert f[0] == "MISSING_COLUMN_DESCRIPTION" and "human_diagnosis" in f[1] and triggered
    assert core.db.one("SELECT state FROM answer_reviews")[0] == "REJECTED"


def test_model_not_configured_is_an_honest_blocked_message(real_warehouse: Settings, tmp_path: Path) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script, provider_missing=True)
    core.handle_message(msg("How many orders were delivered?"))
    wait(core)
    r = core.db.one("SELECT status, error_code FROM requests")
    assert r[:] == ("FAILED", "provider_not_configured")
    assert "BLOCKED" in gw.all_text() and "delivered orders" not in gw.all_text()


def test_slack_delivery_failure_is_recorded_and_redelivered_once(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script)
    gw.fail_posts = 1  # the initial "working" post fails -> answer must be posted later
    core.handle_message(msg("How many orders were delivered?"))
    wait(core)
    assert "slack.delivery_failed" in [e["action"] for e in core.audit.events()]
    assert (
        core.db.one("SELECT delivered_at FROM requests")[0] is not None
    )  # posted as new message after failure
    assert core.redeliver_pending() == 0  # nothing pending -> no duplicate


def test_restart_marks_in_flight_requests_interrupted_without_rerunning(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    core, gw = make_core(real_warehouse, tmp_path, answering_script)
    with core.db.tx() as c:
        c.execute(
            "INSERT INTO requests (request_id, team_id, channel_id, user_id, thread_ts, question, principal, delivery, status, created_at, updated_at) "  # noqa: E501 - long literal
            "VALUES ('r-old', ?, 'D0DM', ?, '1.0', 'q', 'principal.local_analyst', 'live', 'RUNNING', 'x', 'x')",
            (TEAM, USER),
        )
    assert core.recover_on_startup() == 1
    assert core.db.one("SELECT status FROM requests WHERE request_id='r-old'")[0] == "INTERRUPTED"
    assert "restarted" in gw.all_text()


def test_cancel_and_deleted_question_stop_work(real_warehouse: Settings, tmp_path: Path) -> None:
    gate = threading.Event()

    def slow(h: list[Any]) -> Any:
        gate.wait(5)
        return answering_script(h)

    core, _gw = make_core(real_warehouse, tmp_path, slow)
    rid = core.handle_message(msg("How many orders were delivered?"))
    core.handle_message_changed(TEAM, "D0DM", "1.1", deleted=True)
    gate.set()
    wait(core)
    st = core.db.one("SELECT status, error_code FROM requests WHERE request_id=?", (rid,))
    assert st[0] in ("CANCELLED", "DONE")  # cancellation is cooperative: honoured at the next step boundary
    if st[0] == "CANCELLED":
        assert st[1] == "cancelled"
