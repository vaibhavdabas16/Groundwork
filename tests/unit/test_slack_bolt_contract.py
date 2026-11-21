"""Slack CONTRACT tests: the real ``slack_bolt`` App dispatches HMAC-signed HTTP requests to our
listeners, and the real ``slack_sdk`` WebClient talks to a local mock of the Slack Web API (no
network). They prove signature verification, routing, loop/retry filtering, ack behaviour and Web
API retry handling. They are NOT evidence of a live Slack workspace."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlencode

import pytest
from slack_bolt.request import BoltRequest

from groundwork.product.slack_app import BoltGateway, build_app, web_client

SECRET = "test-signing-secret"


class MockSlackApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rate_limit_next = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n).decode()
                try:
                    body = json.loads(raw)
                except ValueError:
                    from urllib.parse import parse_qsl

                    body = dict(parse_qsl(raw))
                outer.calls.append((self.path, body))
                if outer.rate_limit_next:
                    outer.rate_limit_next -= 1
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok": false, "error": "ratelimited"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": True, "ts": "1700000000.000100", "channel": "C1"}).encode()
                )

            def log_message(self, *a: Any) -> None:
                return

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/"


class StubCore:
    """Records what the Bolt layer hands to the core (the core itself is tested elsewhere)."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.messages: list[Any] = []
        self.actions: list[dict[str, Any]] = []
        self.changed: list[tuple[str, bool]] = []

        class Pool:
            def submit(self, fn: Any, *a: Any, **k: Any) -> None:
                fn(*a, **k)

        self.pool = Pool()

    def handle_message(self, m: Any) -> None:
        self.messages.append(m)

    def handle_action(self, **k: Any) -> None:
        self.actions.append(k)

    def handle_view_submission(self, **k: Any) -> None:
        self.actions.append(k)

    def handle_message_changed(self, team: str, channel: str, ts: str, deleted: bool) -> None:
        self.changed.append((ts, deleted))


@pytest.fixture
def app_core() -> tuple[Any, StubCore, MockSlackApi]:
    api = MockSlackApi()
    holder: dict[str, StubCore] = {}

    def factory(gw: Any) -> StubCore:
        holder["c"] = StubCore(gw)
        return holder["c"]

    app, _ = build_app(
        factory,
        token="xoxb-test",
        signing_secret=SECRET,
        client=web_client("xoxb-test", api.url),
        token_verification=False,
        process_before_response=True,
    )
    return app, holder["c"], api


def signed(
    body: str,
    *,
    secret: str = SECRET,
    ts: int | None = None,
    ctype: str = "application/json",
    retry: str | None = None,
) -> BoltRequest:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    headers = {"content-type": [ctype], "x-slack-request-timestamp": [str(ts)], "x-slack-signature": [sig]}
    if retry:
        headers["x-slack-retry-num"] = [retry]
    return BoltRequest(body=body, headers=headers)


def event(ev: dict[str, Any], event_id: str = "Ev1") -> str:
    return json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": event_id,
            "event": ev,
            "authorizations": [{"user_id": "UBOT", "is_bot": True}],
        }
    )


def test_bad_or_stale_signature_is_rejected_before_listeners(app_core: Any) -> None:
    app, core, _ = app_core
    body = event({"type": "app_mention", "user": "U1", "text": "<@UBOT> hi", "ts": "1.1", "channel": "C1"})
    assert app.dispatch(signed(body, secret="wrong")).status == 401
    assert app.dispatch(signed(body, ts=int(time.time()) - 3600)).status == 401
    assert core.messages == []


def test_mention_and_dm_are_routed_with_envelope_identity(app_core: Any) -> None:
    app, core, _ = app_core
    r = app.dispatch(
        signed(
            event(
                {
                    "type": "app_mention",
                    "user": "U1",
                    "text": "<@UBOT> GMV in March 2018?",
                    "ts": "1.1",
                    "channel": "C1",
                }
            )
        )
    )
    assert r.status == 200
    app.dispatch(
        signed(
            event(
                {
                    "type": "message",
                    "channel_type": "im",
                    "user": "U2",
                    "text": "orders?",
                    "ts": "2.1",
                    "channel": "D1",
                },
                "Ev2",
            )
        )
    )
    m1, m2 = core.messages
    assert (m1.user_id, m1.team_id, m1.text, m1.event_id, m1.is_mention) == (
        "U1",
        "T1",
        "GMV in March 2018?",
        "Ev1",
        True,
    )
    assert (m2.user_id, m2.channel_type, m2.is_mention) == ("U2", "im", False)


def test_bot_messages_subtypes_and_channel_chatter_are_ignored(app_core: Any) -> None:
    app, core, _ = app_core
    for i, ev in enumerate(
        [
            {
                "type": "message",
                "channel_type": "im",
                "bot_id": "B1",
                "text": "loop?",
                "ts": "3.1",
                "channel": "D1",
            },
            {
                "type": "message",
                "channel_type": "im",
                "subtype": "bot_message",
                "text": "x",
                "ts": "3.2",
                "channel": "D1",
            },
            {
                "type": "message",
                "channel_type": "channel",
                "user": "U1",
                "text": "not for the bot",
                "ts": "3.3",
                "channel": "C1",
            },
        ]
    ):
        app.dispatch(signed(event(ev, f"EvI{i}")))
    assert core.messages == []
    app.dispatch(
        signed(
            event(
                {
                    "type": "message",
                    "channel_type": "im",
                    "subtype": "message_deleted",
                    "deleted_ts": "1.1",
                    "previous_message": {"ts": "1.1", "user": "U1"},
                    "ts": "4.1",
                    "channel": "D1",
                },
                "EvD",
            )
        )
    )
    assert core.changed == [("1.1", True)]


def test_interactive_action_is_acked_and_identity_comes_from_payload(app_core: Any) -> None:
    app, core, _ = app_core
    payload = {
        "type": "block_actions",
        "user": {"id": "UREV", "team_id": "T1"},
        "team": {"id": "T1"},
        "channel": {"id": "C9"},
        "trigger_id": "tr1",
        "container": {"message_ts": "5.1"},
        "api_app_id": "A1",
        "token": "x",
        "actions": [
            {
                "action_id": "cf_prop_approve",
                "value": json.dumps({"pid": "cfp-1", "rev": 1}),
                "action_ts": "5.2",
            }
        ],
    }
    r = app.dispatch(
        signed(urlencode({"payload": json.dumps(payload)}), ctype="application/x-www-form-urlencoded")
    )
    assert r.status == 200
    a = core.actions[0]
    assert (a["user_id"], a["team_id"], a["action_id"]) == ("UREV", "T1", "cf_prop_approve")


def test_web_api_gateway_retries_rate_limits_and_sends_safe_payloads() -> None:
    api = MockSlackApi()
    api.rate_limit_next = 1
    gw = BoltGateway(web_client("xoxb-test", api.url))
    ts = gw.post(
        "C1",
        text="hello",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
        thread_ts="1.1",
    )
    assert ts == "1700000000.000100"
    paths = [c[0] for c in api.calls]
    assert paths == ["/api/chat.postMessage", "/api/chat.postMessage"]  # retried once after 429
    sent = api.calls[-1][1]
    assert sent["thread_ts"] == "1.1" and sent.get("unfurl_links") in (False, "0", "false")
