"""TEST DOUBLE - scripted provider for unit tests and fault injection ONLY.

It is not a model. It is never selectable from the CLI, it reports ``live = False``, and the
evaluation harness labels any run that uses it as ``NOT A MODEL EVALUATION``. A script is a
callable that receives the conversation so far and returns the next turn, so tests can exercise
runtime control flow (malformed arguments, loops, timeouts, rate limits) deterministically.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from groundwork.agent.providers.base import (
    ModelProvider,
    ModelTurn,
    ProviderSession,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolSpec,
    TurnUsage,
)

History = list[list[TextPart | ToolResultPart]]
Script = Callable[[History], Awaitable[ModelTurn] | ModelTurn]
_ids = itertools.count(1)


def turn(
    *calls: tuple[str, dict[str, Any] | None], text: str = "", stop_reason: str = "tool_use"
) -> ModelTurn:
    return ModelTurn(
        text=text,
        tool_calls=[ToolCall(id=f"call_{next(_ids)}", name=n, arguments=a) for n, a in calls],
        stop_reason=stop_reason if calls else "end_turn",
        usage=TurnUsage(input_tokens=100, output_tokens=20),
        resolved_model="scripted-test",
        provider_request_id=None,
        latency_ms=0.0,
    )


class ScriptedSession(ProviderSession):
    def __init__(self, script: Script) -> None:
        self.script = script
        self.history: History = []
        self.system: str = ""
        self.tools: list[ToolSpec] = []

    async def send(self, parts: list[TextPart | ToolResultPart]) -> ModelTurn:
        self.history.append(list(parts))
        out = self.script(self.history)
        if asyncio.iscoroutine(out) or isinstance(out, Awaitable):
            return await out
        return out


class ScriptedProvider(ModelProvider):
    name = "scripted-test"
    live = False

    def __init__(self, script: Script) -> None:
        self.script = script
        self.sessions: list[ScriptedSession] = []

    def start(self, system: str, tools: list[ToolSpec]) -> ProviderSession:
        s = ScriptedSession(self.script)
        s.system, s.tools = system, tools
        self.sessions.append(s)
        return s

    def parameters(self) -> dict[str, Any]:
        return {"test_double": True}
