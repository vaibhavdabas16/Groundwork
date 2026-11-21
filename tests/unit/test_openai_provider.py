"""SDK-level CONTRACT tests for the OpenAI adapter.

These run the real ``openai`` SDK (request building, response parsing, typed errors) against an
in-process HTTP mock transport. They prove the adapter's wire contract and error handling; they are
NOT evidence of live model behaviour and make no network calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import openai
import pytest

from groundwork.agent import pricing
from groundwork.agent.providers.base import (
    ProviderError,
    ProviderNotConfigured,
    TextPart,
    ToolResultPart,
    ToolSpec,
)
from groundwork.agent.providers.openai_provider import DEFAULT_BASE_URL, OpenAIProvider
from groundwork.agent.providers.registry import make_provider
from groundwork.agent.schemas import AgentConfig, UsageInfo

TOOLS = [
    ToolSpec(
        "run_query",
        "run sql",
        {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    )
]


def _cfg(**kw: Any) -> AgentConfig:
    return AgentConfig(provider="openai", model=kw.pop("model", "gpt-5.5"), **kw)


def _response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    incomplete: str | None = None,
    refusal: str | None = None,
    model: str = "gpt-5.5-2026-04-23",
) -> dict[str, Any]:
    """A Responses API ``response`` object (shape per the installed SDK's ``types/responses``)."""
    output: list[dict[str, Any]] = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-opaque"}
    ]
    parts: list[dict[str, Any]] = []
    if content is not None:
        parts.append({"type": "output_text", "text": content, "annotations": []})
    if refusal is not None:
        parts.append({"type": "refusal", "refusal": refusal})
    if parts:
        output.append(
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": parts}
        )
    output.extend(tool_calls or [])
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "incomplete" if incomplete else "completed",
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "error": None,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1500,
            "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
            "output_tokens": 90,
            "output_tokens_details": {"reasoning_tokens": 30},
            "total_tokens": 1590,
        },
    }


def _call(cid: str, args: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{cid}",
        "call_id": cid,
        "name": "run_query",
        "arguments": args,
        "status": "completed",
    }


class Recorder:
    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.urls.append(str(request.url))
        return self.responses.pop(0)


def provider(rec: Recorder, **cfg: Any) -> OpenAIProvider:
    client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(rec))
    return OpenAIProvider(_cfg(**cfg), api_key="test-key-not-real", http_client=client)


def test_missing_key_is_not_configured_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "present-but-irrelevant")
    with pytest.raises(ProviderNotConfigured) as ei:
        make_provider(_cfg())
    assert ei.value.missing == ["OPENAI_API_KEY"]


def test_claude_model_with_openai_provider_is_a_config_error() -> None:
    with pytest.raises(ProviderError) as ei:
        OpenAIProvider(AgentConfig(provider="openai"), api_key="x")
    assert ei.value.kind == "config"


def test_ambient_base_url_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    monkeypatch.delenv("GROUNDWORK_OPENAI_BASE_URL", raising=False)
    rec = Recorder([httpx2.Response(200, json=_response(content="ok"))])
    p = provider(rec)
    asyncio.run(p.start("S", TOOLS).send([TextPart("q")]))
    assert rec.urls[0].startswith(DEFAULT_BASE_URL)


def test_request_shape_tool_call_parsing_and_usage() -> None:
    rec = Recorder(
        [
            httpx2.Response(
                200,
                json=_response(content="Checking.", tool_calls=[_call("call_1", '{"sql": "SELECT 1"}')]),
                headers={"x-request-id": "req_abc"},
            ),
            httpx2.Response(200, json=_response(content="done")),
        ]
    )
    p = provider(rec, effort="high")

    async def go() -> Any:
        s = p.start("SYSTEM", TOOLS)
        t1 = await s.send([TextPart("q")])
        t2 = await s.send([ToolResultPart("call_1", '{"ok": true}', False)])
        return t1, t2

    t1, t2 = asyncio.run(go())
    assert rec.urls[0].endswith("/responses")
    body = rec.requests[0]
    assert body["model"] == "gpt-5.5"
    assert body["reasoning"] == {"effort": "high"}
    assert body["store"] is False and "previous_response_id" not in body
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["max_output_tokens"] == 8000
    assert "temperature" not in body
    assert body["instructions"] == "SYSTEM"
    assert body["input"] == [{"role": "user", "content": "q"}]
    tool = body["tools"][0]
    assert tool["type"] == "function" and tool["name"] == "run_query" and tool["strict"] is False
    assert t1.tool_calls[0].name == "run_query" and t1.tool_calls[0].arguments == {"sql": "SELECT 1"}
    assert t1.tool_calls[0].id == "call_1"  # call_id (not the item id) pairs the result
    assert t1.text == "Checking." and t1.stop_reason == "tool_use"
    # input_tokens includes cached tokens; they are split out so cost is not double-counted
    assert t1.usage.input_tokens == 1500 - 1024 and t1.usage.cache_read_input_tokens == 1024
    assert t1.usage.output_tokens == 90
    assert t1.resolved_model == "gpt-5.5-2026-04-23" and t1.provider_request_id == "req_abc"
    items = rec.requests[1]["input"]
    # stateless replay: user turn, then every output item unchanged, then the tool result
    assert [i.get("type", "user") for i in items] == [
        "user",
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert items[1]["encrypted_content"] == "enc-opaque"
    assert items[3]["call_id"] == "call_1"
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}
    assert t2.stop_reason == "end_turn"


def test_malformed_tool_arguments_are_flagged_not_crashed() -> None:
    rec = Recorder(
        [httpx2.Response(200, json=_response(tool_calls=[_call("c1", "{not json"), _call("c2", "[1]")]))]
    )
    t = asyncio.run(provider(rec).start("S", TOOLS).send([TextPart("q")]))
    assert [c.arguments for c in t.tool_calls] == [None, None]
    assert all(c.raw_error for c in t.tool_calls)


def test_temperature_is_sent_only_when_configured() -> None:
    rec = Recorder([httpx2.Response(200, json=_response(content="ok"))])
    asyncio.run(provider(rec, temperature=0.0).start("S", TOOLS).send([TextPart("q")]))
    assert rec.requests[0]["temperature"] == 0.0


def _err(status: int, headers: dict[str, str] | None = None, code: str = "x") -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"error": {"message": "boom", "type": "x", "code": code, "param": None}},
        headers=headers or {},
    )


def test_rate_limit_and_5xx_are_retried_and_counted() -> None:
    rec = Recorder(
        [
            _err(429, {"retry-after": "0"}),
            _err(503, {"retry-after": "0"}),
            httpx2.Response(200, json=_response(content="ok")),
        ]
    )
    t = asyncio.run(provider(rec, max_provider_retries=3).start("S", TOOLS).send([TextPart("q")]))
    assert t.retries == 2 and len(rec.requests) == 3


def test_retries_are_bounded() -> None:
    rec = Recorder([_err(429, {"retry-after": "0"}) for _ in range(3)])
    with pytest.raises(ProviderError) as ei:
        asyncio.run(provider(rec, max_provider_retries=2).start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == "rate_limited" and len(rec.requests) == 3


def test_insufficient_quota_is_not_retried() -> None:
    rec = Recorder([_err(429, code="insufficient_quota")])
    with pytest.raises(ProviderError) as ei:
        asyncio.run(provider(rec, max_provider_retries=3).start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == "auth" and len(rec.requests) == 1


@pytest.mark.parametrize(
    ("status", "kind"), [(400, "bad_request"), (401, "auth"), (403, "auth"), (404, "not_found")]
)
def test_non_retryable_errors_fail_fast(status: int, kind: str) -> None:
    rec = Recorder([_err(status)])
    with pytest.raises(ProviderError) as ei:
        asyncio.run(provider(rec, max_provider_retries=3).start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == kind and len(rec.requests) == 1


def test_timeout_is_typed_and_bounded() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated", request=request)

    client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(boom))
    p = OpenAIProvider(_cfg(max_provider_retries=0), api_key="x", http_client=client)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(p.start("S", TOOLS).send([TextPart("q")]))
    assert ei.value.kind == "timeout"


@pytest.mark.parametrize(
    ("payload", "stop"),
    [
        ({"refusal": "I can't help with that."}, "refusal"),
        ({"incomplete": "content_filter"}, "refusal"),
        ({"content": "partial", "incomplete": "max_output_tokens"}, "max_tokens"),
    ],
)
def test_refusal_filter_and_truncation_are_normalised(payload: dict[str, Any], stop: str) -> None:
    rec = Recorder([httpx2.Response(200, json=_response(**payload))])
    t = asyncio.run(provider(rec).start("S", TOOLS).send([TextPart("q")]))
    assert t.stop_reason == stop


def test_openai_models_have_no_guessed_price_but_operator_file_works(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(pricing.PRICES_FILE_ENV, raising=False)
    usage = UsageInfo(input_tokens=1_000_000, output_tokens=1_000_000)
    assert pricing.estimate_cost_usd("gpt-5.5", usage) is None and not pricing.has_price("gpt-5.5")
    f = tmp_path / "prices.json"
    f.write_text(
        json.dumps(
            {
                "source": "test",
                "models": {
                    "gpt-5.5": {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cache_read_per_mtok": 0.1}
                },
            }
        )
    )
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(f))
    assert pricing.estimate_cost_usd("gpt-5.5", usage) == 3.0
    assert "+operator:" in pricing.pricing_version()


def test_budgeted_run_on_unpriced_model_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """A budget that cannot be enforced (unknown price) must BLOCK, not silently never trigger."""
    from groundwork.config.settings import ContextMode, get_settings
    from groundwork.eval.harness import EvalBlocked, RunPlan, run_experiment

    monkeypatch.delenv(pricing.PRICES_FILE_ENV, raising=False)
    p = OpenAIProvider(_cfg(), api_key="x")
    plan = RunPlan(modes=[ContextMode.full_context], budget_usd=5.0)
    with pytest.raises(EvalBlocked, match="cannot be enforced"):
        asyncio.run(run_experiment(get_settings(), plan, _cfg(), p))
