"""SDK-level CONTRACT tests for the Anthropic adapter.

These run the real ``anthropic`` SDK (request building, response parsing, typed errors) against an
in-process HTTP mock transport. They prove the adapter's wire contract and error handling; they are
NOT evidence of live model behaviour and make no network calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic
import httpx2
import pytest

from groundwork.agent.providers.anthropic_provider import AnthropicProvider
from groundwork.agent.providers.base import (
    ProviderError,
    ProviderNotConfigured,
    TextPart,
    ToolResultPart,
    ToolSpec,
)
from groundwork.agent.schemas import AgentConfig

TOOLS = [
    ToolSpec(
        "run_query",
        "run sql",
        {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    )
]


def _msg(
    content: list[dict[str, Any]], stop: str = "tool_use", model: str = "claude-opus-5-5"
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 80,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 0,
        },
    }


class Recorder:
    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0)


def provider(rec: Recorder, **cfg: Any) -> AnthropicProvider:
    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(rec))
    return AnthropicProvider(AgentConfig(**cfg), api_key="test-key-not-real", http_client=client)


def test_missing_key_is_not_configured_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured) as ei:
        AnthropicProvider(AgentConfig())
    assert ei.value.missing == ["ANTHROPIC_API_KEY"]


def test_temperature_is_a_config_error() -> None:
    with pytest.raises(ProviderError) as ei:
        AnthropicProvider(AgentConfig(temperature=0.0), api_key="x")
    assert ei.value.kind == "config"


def test_request_shape_tool_use_parsing_and_usage() -> None:
    rec = Recorder(
        [
            httpx2.Response(
                200,
                json=_msg(
                    [
                        {"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "text", "text": "Checking."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "run_query",
                            "input": {"sql": "SELECT 1"},
                        },
                    ]
                ),
                headers={"request-id": "req_abc"},
            ),
            httpx2.Response(200, json=_msg([{"type": "text", "text": "done"}], stop="end_turn")),
        ]
    )
    p = provider(rec, effort="high")

    async def go() -> Any:
        s = p.start("SYSTEM", TOOLS)
        t1 = await s.send([TextPart("q")])
        t2 = await s.send([ToolResultPart("toolu_1", '{"ok": true}', False)])
        return t1, t2

    t1, t2 = asyncio.run(go())
    body = rec.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["system"] == "SYSTEM"
    assert "temperature" not in body and "fallbacks" not in body
    assert body["tools"][0]["name"] == "run_query"
    assert t1.tool_calls[0].name == "run_query" and t1.tool_calls[0].arguments == {"sql": "SELECT 1"}
    assert t1.text == "Checking." and t1.stop_reason == "tool_use"
    assert t1.usage.input_tokens == 1200 and t1.usage.cache_creation_input_tokens == 1000
    assert t1.resolved_model == "claude-opus-5-5" and t1.provider_request_id == "req_abc"
    # second request replays the assistant turn unchanged (thinking block included) + tool_result
    msgs = rec.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0]["type"] == "thinking" and msgs[1]["content"][0]["signature"] == "sig"
    assert msgs[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": '{"ok": true}',
        "is_error": False,
    }
    assert t2.stop_reason == "end_turn"


def _err(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": "x", "message": "boom"}}, headers=headers or {}
    )


def test_rate_limit_is_retried_with_retry_after_and_counted() -> None:
    rec = Recorder(
        [
            _err(429, {"retry-after": "0"}),
            _err(529),
            httpx2.Response(200, json=_msg([{"type": "text", "text": "ok"}], stop="end_turn")),
        ]
    )
    p = provider(rec, max_provider_retries=3)

    async def go() -> Any:
        return await p.start("S", TOOLS).send([TextPart("q")])

    t = asyncio.run(go())
    assert t.retries == 2 and len(rec.requests) == 3


def test_retries_are_bounded() -> None:
    rec = Recorder([_err(429, {"retry-after": "0"}) for _ in range(3)])
    p = provider(rec, max_provider_retries=2)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(p.start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == "rate_limited" and len(rec.requests) == 3


@pytest.mark.parametrize(
    ("status", "kind"), [(400, "bad_request"), (401, "auth"), (403, "auth"), (404, "not_found")]
)
def test_non_retryable_errors_fail_fast(status: int, kind: str) -> None:
    rec = Recorder([_err(status)])
    p = provider(rec, max_provider_retries=3)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(p.start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == kind and len(rec.requests) == 1


def test_timeout_is_typed_and_bounded() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated", request=request)

    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(boom))
    p = AnthropicProvider(AgentConfig(max_provider_retries=0), api_key="x", http_client=client)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(p.start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == "timeout"


def test_refusal_and_truncation_stop_reasons_surface() -> None:
    rec = Recorder([httpx2.Response(200, json=_msg([{"type": "text", "text": ""}], stop="refusal"))])
    t = asyncio.run(provider(rec).start("S", TOOLS).send([TextPart("q")]))
    assert t.stop_reason == "refusal"
