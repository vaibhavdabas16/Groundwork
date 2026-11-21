"""Slack Bolt binding for ``ProductCore``.

* Socket Mode (default; no public endpoint): ``SLACK_BOT_TOKEN`` (xoxb) + ``SLACK_APP_TOKEN`` (xapp,
  ``connections:write``). HTTP mode: ``SLACK_BOT_TOKEN`` + ``SLACK_SIGNING_SECRET``; Bolt verifies the
  ``X-Slack-Signature`` HMAC and timestamp on every request before any listener runs.
* Every handler ``ack()``s immediately and hands work to the core's worker pool, so Slack's 3-second
  deadline is met and long agent runs never cause Slack to retry.
* Retries: Slack redelivers an event with the SAME ``event_id``; the core dedupes on it. In HTTP mode
  the ``X-Slack-Retry-Num`` header is also honoured by Bolt.
* Loops: messages with ``bot_id``, any ``subtype`` other than edits/deletions, or from the bot's own
  user id are ignored.
* Web API calls use slack_sdk retry handlers (rate limits with Retry-After, connection errors, 5xx).
"""

from __future__ import annotations

import os
import re
from typing import Any

from slack_bolt import App
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
    ServerErrorRetryHandler,
)

from groundwork.observability.logging import get_logger
from groundwork.product.core import IncomingMessage, ProductCore

log = get_logger("slack")
_MENTION = re.compile(r"<@[UW][A-Z0-9]+>")


class SlackNotConfigured(RuntimeError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"Slack not configured; missing {missing}")
        self.missing = missing


def web_client(token: str, base_url: str | None = None) -> WebClient:
    kw: dict[str, Any] = {"token": token}
    if base_url:
        kw["base_url"] = base_url
    client = WebClient(**kw)
    client.retry_handlers.extend(
        [
            RateLimitErrorRetryHandler(max_retry_count=3),
            ServerErrorRetryHandler(max_retry_count=2),
            ConnectionErrorRetryHandler(max_retry_count=2),
        ]
    )
    return client


class BoltGateway:
    """``SlackGateway`` over the Slack Web API. Raises on API failure (the core records it)."""

    def __init__(self, client: WebClient) -> None:
        self.client = client

    def post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str:
        r = self.client.chat_postMessage(
            channel=channel,
            text=text[:3000],
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        return str(r["ts"])

    def update(self, channel: str, ts: str, *, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        self.client.chat_update(channel=channel, ts=ts, text=text[:3000], blocks=blocks or [])

    def ephemeral(
        self,
        channel: str,
        user: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> None:
        self.client.chat_postEphemeral(
            channel=channel, user=user, text=text[:3000], blocks=blocks, thread_ts=thread_ts
        )

    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> None:
        self.client.views_open(trigger_id=trigger_id, view=view)


def build_app(
    core_factory: Any,
    *,
    token: str | None = None,
    signing_secret: str | None = None,
    client: WebClient | None = None,
    token_verification: bool = True,
    process_before_response: bool = False,
) -> tuple[App, ProductCore]:
    """Create the Bolt app. ``core_factory(gateway) -> ProductCore``."""
    token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackNotConfigured(["SLACK_BOT_TOKEN"])
    client = client or web_client(token)
    app = App(
        token=token,
        signing_secret=signing_secret or os.environ.get("SLACK_SIGNING_SECRET") or "socket-mode-unused",
        client=client,
        token_verification_enabled=token_verification,
        process_before_response=process_before_response,
    )
    core: ProductCore = core_factory(BoltGateway(client))

    def bot_user(context: Any) -> str | None:
        return getattr(context, "bot_user_id", None) or (
            context.get("bot_user_id") if hasattr(context, "get") else None
        )

    def to_msg(body: dict[str, Any], event: dict[str, Any], *, mention: bool) -> IncomingMessage:
        return IncomingMessage(
            team_id=str(body.get("team_id") or event.get("team") or ""),
            channel_id=str(event["channel"]),
            user_id=str(event["user"]),
            text=_MENTION.sub("", str(event.get("text") or "")).strip(),
            ts=str(event["ts"]),
            thread_ts=event.get("thread_ts"),
            event_id=str(body.get("event_id") or f"{event['channel']}:{event['ts']}"),
            channel_type=str(event.get("channel_type") or ("channel" if mention else "im")),
            is_mention=mention,
        )

    @app.event("app_mention")
    def on_mention(body: dict[str, Any], event: dict[str, Any], context: Any) -> None:
        if event.get("bot_id") or event.get("user") in (None, bot_user(context)):
            return
        core.handle_message(to_msg(body, event, mention=True))

    @app.event("message")
    def on_message(body: dict[str, Any], event: dict[str, Any], context: Any) -> None:
        sub = event.get("subtype")
        if sub in ("message_changed", "message_deleted"):
            prev = event.get("previous_message") or event.get("message") or {}
            ts = prev.get("ts") or event.get("deleted_ts")
            if ts and prev.get("user") != bot_user(context):
                core.handle_message_changed(
                    str(body.get("team_id") or ""),
                    str(event.get("channel")),
                    str(ts),
                    sub == "message_deleted",
                )
            return
        if sub or event.get("bot_id") or event.get("user") in (None, bot_user(context)):
            return
        if event.get("channel_type") != "im":  # channel follow-ups must @mention the bot (minimal scopes)
            return
        core.handle_message(to_msg(body, event, mention=False))

    @app.action(re.compile(r"^cf_"))
    def on_action(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
        ack()
        user = body.get("user") or {}
        container = body.get("container") or {}
        key = f"{body.get('trigger_id') or ''}:{action.get('action_ts') or ''}:{user.get('id')}:{action.get('action_id')}"
        kwargs: dict[str, Any] = {
            "action_id": str(action["action_id"]),
            "value": str(action.get("value") or ""),
            "user_id": str(user.get("id")),
            "team_id": str((body.get("team") or {}).get("id") or user.get("team_id") or ""),
            "channel_id": str((body.get("channel") or {}).get("id") or container.get("channel_id") or ""),
            "trigger_id": body.get("trigger_id"),
            "dedupe_key": key,
            "message_ts": container.get("message_ts"),
        }
        if kwargs["action_id"].endswith(("_reject", "_clarify", "_changes", "_down")):
            core.handle_action(**kwargs)  # opens a modal: trigger_id expires after 3 s, run inline
        else:
            core.pool.submit(core.handle_action, **kwargs)

    @app.view(re.compile(r"^cf_"))
    def on_view(ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
        ack()
        user = body.get("user") or {}
        core.pool.submit(
            core.handle_view_submission,
            callback_id=str(view["callback_id"]),
            private_metadata=str(view.get("private_metadata") or ""),
            values=(view.get("state") or {}).get("values") or {},
            user_id=str(user.get("id")),
            team_id=str((body.get("team") or {}).get("id") or user.get("team_id") or ""),
            dedupe_key=f"{view.get('id')}:{view.get('hash')}",
        )

    @app.error
    def on_error(error: Exception, body: dict[str, Any]) -> None:
        log.error("bolt listener error", extra={"error": type(error).__name__})

    return app, core


def run_socket_mode(app: App) -> None:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise SlackNotConfigured(["SLACK_APP_TOKEN"])
    SocketModeHandler(app, app_token).start()  # type: ignore[no-untyped-call]
