"""The agent's ONLY route to context and data: the Phase 1 MCP server over stdio.

The server process is spawned with server-side configuration (principal, context mode, paths).
The agent cannot change identity or context exposure through tool arguments; the experiment
condition is fixed when the server starts, at the trusted service boundary.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from groundwork.config.settings import ContextMode, Settings

AGENT_TOOLS = (
    "search_assets",
    "get_asset",
    "get_definition",
    "get_join_path",
    "get_policy",
    "explain_query",
    "run_query",
)


class McpUnavailable(RuntimeError):
    """The MCP server could not be started or stopped responding (context source unavailable)."""


@dataclass
class McpToolResult:
    ok: bool  # protocol-level success AND envelope ok
    protocol_error: bool  # argument validation / unknown tool / transport error
    data: dict[str, Any]
    error_code: str | None
    error_message: str | None
    duration_ms: float


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


def server_parameters(
    settings: Settings,
    *,
    mode: ContextMode,
    principal: str,
    ops_dir: Path,
    context_dir: Path | None = None,
) -> StdioServerParameters:
    env = {
        "GROUNDWORK_PRINCIPAL": principal,
        "GROUNDWORK_CONTEXT_MODE": mode.value,
        "GROUNDWORK_DATA_DIR": str(ops_dir),  # private audit DB per server process
        "GROUNDWORK_SERVING_PATH": str(settings.serving_path),
        "GROUNDWORK_WAREHOUSE_PATH": str(settings.warehouse_path),
        "GROUNDWORK_CONTEXT_DIR": str(context_dir or settings.context_dir),
        "GROUNDWORK_ROOT": str(settings.root),
        "PYTHONIOENCODING": "utf-8",
    }
    for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "groundwork.mcp_server.server"],
        env=env,
        cwd=str(settings.root),
    )


class McpToolClient:
    """Async context manager owning one MCP server subprocess and session."""

    def __init__(self, params: StdioServerParameters, *, call_timeout_seconds: float = 60.0) -> None:
        self.params = params
        self.call_timeout = call_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: dict[str, McpTool] = {}
        self.server_name: str | None = None
        self._errlog: Any = None

    async def __aenter__(self) -> McpToolClient:
        self._stack = AsyncExitStack()
        try:
            self._errlog = open(os.devnull, "w")  # noqa: SIM115 - closed in __aexit__
            self._stack.callback(self._errlog.close)
            read, write = await self._stack.enter_async_context(
                stdio_client(self.params, errlog=self._errlog)
            )
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            init = await self.session.initialize()
            self.server_name = getattr(getattr(init, "server_info", None), "name", None)
            listed = await self.session.list_tools()
        except BaseException as exc:
            await self._stack.aclose()
            if isinstance(exc, Exception):
                raise McpUnavailable(f"MCP server unavailable: {type(exc).__name__}: {exc}") from exc
            raise
        self.tools = {
            t.name: McpTool(t.name, t.description or "", dict(t.input_schema)) for t in listed.tools
        }
        missing = [n for n in AGENT_TOOLS if n not in self.tools]
        if missing:
            await self._stack.aclose()
            raise McpUnavailable(f"MCP server is missing required tools: {missing}")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def call(
        self, name: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> McpToolResult:
        if self.session is None:
            raise McpUnavailable("MCP session is not open")
        t0 = time.perf_counter()
        try:
            res = await self.session.call_tool(
                name, arguments, read_timeout_seconds=min(timeout or self.call_timeout, self.call_timeout)
            )
        except Exception as exc:  # transport failure or request timeout
            ms = (time.perf_counter() - t0) * 1000.0
            msg = f"{type(exc).__name__}: {exc}"[:300]
            code = (
                "mcp_timeout"
                if "timeout" in msg.lower() or "timed out" in msg.lower()
                else "mcp_transport_error"
            )
            return McpToolResult(False, True, {}, code, msg, ms)
        ms = (time.perf_counter() - t0) * 1000.0
        structured = getattr(res, "structured_content", None)
        if getattr(res, "is_error", False) or not isinstance(structured, dict):
            texts = [getattr(c, "text", "") for c in getattr(res, "content", []) or []]
            return McpToolResult(False, True, {}, "invalid_tool_arguments", " ".join(texts)[:500], ms)
        err = structured.get("error") or {}
        return McpToolResult(
            bool(structured.get("ok")),
            False,
            structured,
            err.get("code"),
            err.get("message"),
            ms,
        )
