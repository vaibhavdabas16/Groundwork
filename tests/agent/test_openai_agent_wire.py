"""OpenAI adapter driven by the REAL agent runtime and REAL MCP server, with the HTTP layer replaced
by an in-process mock transport (no network, no key). Proves the Responses API item sequence
the runtime produces is well-formed (every function call answered by its output, in order) and that
provenance records the OpenAI provider. NOT evidence of live model behaviour.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from contextforge.agent.factory import open_agent
from contextforge.agent.providers.openai_provider import OpenAIProvider
from contextforge.agent.schemas import AgentConfig, Outcome
from contextforge.agent.traces import TraceStore
from contextforge.config.settings import ContextMode, Settings

pytestmark = [pytest.mark.integration, pytest.mark.agent]

SQL = "SELECT count(*) AS delivered FROM analytics.orders WHERE order_status = 'delivered'"


def _response(tool_name: str, args: dict[str, Any], cid: str) -> dict[str, Any]:
    return {
        "id": "resp_x",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5.5-2026-04-23",
        "status": "completed",
        "incomplete_details": None,
        "error": None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {"type": "reasoning", "id": f"rs_{cid}", "summary": [], "encrypted_content": "enc"},
            {
                "type": "function_call",
                "id": f"fc_{cid}",
                "call_id": cid,
                "name": tool_name,
                "arguments": json.dumps(args),
                "status": "completed",
            },
        ],
        "usage": {
            "input_tokens": 900,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 40,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 940,
        },
    }


def _assert_well_formed(items: list[dict[str, Any]]) -> None:
    assert items and items[0].get("role") == "user"
    pending: list[str] = []
    for it in items:
        kind = it.get("type")
        if kind == "function_call_output":
            assert pending and it["call_id"] == pending.pop(0)
            continue
        if kind == "function_call":
            pending.append(it["call_id"])
            continue
        if kind == "reasoning":
            assert it.get("encrypted_content"), "reasoning must be replayed with encrypted content"
            assert not pending
            continue
        assert not pending, f"function calls {pending} not answered before a {kind or it.get('role')} item"


def test_openai_adapter_through_runtime_and_mcp(real_warehouse: Settings, tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert body["store"] is False
        _assert_well_formed(body["input"])
        if len(bodies) == 1:
            return httpx2.Response(200, json=_response("run_query", {"sql": SQL}, "call_q"))
        tool_out = json.loads(body["input"][-1]["output"])["untrusted_tool_output"]
        return httpx2.Response(
            200,
            json=_response(
                "submit_response",
                {"outcome": "ANSWER", "answer": "delivered orders counted", "query_id": tool_out["query_id"]},
                "call_s",
            ),
        )

    cfg = AgentConfig(provider="openai", model="gpt-5.5")
    client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    prov = OpenAIProvider(cfg, api_key="test-key-not-real", http_client=client)

    async def main() -> Any:
        try:
            async with open_agent(
                real_warehouse,
                cfg,
                mode=ContextMode.full_context,
                provider=prov,
                trace_store=TraceStore(tmp_path / "traces.sqlite"),
                ops_dir=tmp_path / "ops",
            ) as agent:
                return await agent.ask("How many orders were delivered?")
        finally:
            await prov.aclose()

    res = asyncio.run(main())
    r = res.response
    assert r.outcome == Outcome.ANSWER, r.error
    assert r.executed_sql and r.result_row_count == 1
    assert r.model.provider == "openai" and r.model.live
    assert r.model.requested_model == "gpt-5.5" and r.model.resolved_models[0] == "gpt-5.5-2026-04-23"
    assert r.model.parameters["store"] is False
    assert r.usage.input_tokens == 1800 and r.usage.model_calls == 2
    assert r.cost.estimated_usd is None  # no guessed OpenAI price
    assert len(bodies) == 2
