"""Wiring: provider + MCP server subprocess + trace store -> DataAgent."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from groundwork.agent.mcp_client import McpToolClient, server_parameters
from groundwork.agent.provenance import provenance
from groundwork.agent.providers.base import ModelProvider
from groundwork.agent.providers.registry import make_provider
from groundwork.agent.runtime import DataAgent
from groundwork.agent.schemas import AgentConfig
from groundwork.agent.traces import TraceStore
from groundwork.config.settings import ContextMode, Settings


def default_trace_store(settings: Settings) -> TraceStore:
    return TraceStore(settings.data_dir / "ops" / "agent_traces.sqlite")


@asynccontextmanager
async def open_agent(
    settings: Settings,
    config: AgentConfig,
    *,
    mode: ContextMode,
    principal: str | None = None,
    provider: ModelProvider | None = None,
    trace_store: TraceStore | None = None,
    ops_dir: Path | None = None,
    context_dir: Path | None = None,
) -> AsyncIterator[DataAgent]:
    """Start an MCP server for ``mode`` and yield an agent bound to it.

    ``provider`` defaults to ``make_provider(config)``, which raises ``ProviderNotConfigured`` when
    credentials are missing (callers must report BLOCKED; nothing falls back to a test double).
    """
    prov = provider or make_provider(config)
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if ops_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="cf-mcp-")
        ops_dir = Path(tmp.name)
    params = server_parameters(
        settings,
        mode=mode,
        principal=principal or settings.principal_id,
        ops_dir=ops_dir,
        context_dir=context_dir,
    )
    try:
        async with McpToolClient(params, call_timeout_seconds=config.mcp_call_timeout_seconds) as mcp:
            yield DataAgent(
                prov,
                mcp,
                config,
                mode=mode,
                trace_store=trace_store,
                provenance=provenance(settings),
            )
    finally:
        if provider is None:
            await prov.aclose()
        if tmp is not None:
            tmp.cleanup()
