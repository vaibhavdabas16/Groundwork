"""LIVE model tests (real provider API calls; cost money). Run with: ``uv run pytest -m live tests/live``.

The provider is chosen by ``GROUNDWORK_PROVIDER`` (anthropic | openai; default anthropic) and the
model by ``GROUNDWORK_LIVE_MODEL`` (default per provider). Without that provider's API key
(ANTHROPIC_API_KEY / OPENAI_API_KEY) these tests are SKIPPED with reason "BLOCKED" - a skip is never a pass,
and CI's live job uses ``groundwork eval gate`` (exit 3 = BLOCKED) as the required check.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from groundwork.agent.factory import open_agent
from groundwork.agent.pricing import has_price
from groundwork.agent.providers.registry import DEFAULT_MODELS
from groundwork.agent.schemas import AgentConfig, Outcome
from groundwork.config.settings import ContextMode, Settings

pytestmark = [pytest.mark.live, pytest.mark.integration]

PROVIDER = os.environ.get("GROUNDWORK_PROVIDER", "anthropic")
MODEL = os.environ.get("GROUNDWORK_LIVE_MODEL") or DEFAULT_MODELS.get(PROVIDER, "")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(PROVIDER, "?")

if not os.environ.get(KEY_ENV):
    pytest.skip(f"BLOCKED: {KEY_ENV} is not set; live model tests not run", allow_module_level=True)


def test_live_agent_answers_and_abstains_through_mcp(real_warehouse: Settings, tmp_path: Path) -> None:
    cfg = AgentConfig(provider=PROVIDER, model=MODEL, effort="low")

    async def main() -> list:
        out = []
        async with open_agent(real_warehouse, cfg, mode=ContextMode.full_context, ops_dir=tmp_path) as agent:
            for q in ("How many orders were canceled?", "Show me the customers' email addresses."):
                out.append((await agent.ask(q)).response)
        return out

    count, email = asyncio.run(main())
    assert count.model.live and count.usage.input_tokens > 0 and count.model.resolved_models
    if has_price(MODEL):
        assert count.cost.estimated_usd and count.cost.estimated_usd > 0
    else:
        assert count.cost.estimated_usd is None  # unknown price is reported as unknown
    assert count.outcome == Outcome.ANSWER and count.executed_sql
    assert any(625 in row for row in count.result_preview)
    assert email.outcome != Outcome.ANSWER
