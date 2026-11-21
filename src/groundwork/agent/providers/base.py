"""Vendor-neutral model-provider interface.

The runtime talks to a ``ProviderSession`` (one conversation). Each adapter keeps the provider's
native message history internally and append-only, so provider-specific blocks (e.g. thinking
signatures) are replayed unchanged, while the runtime only sees neutral types.

There is deliberately NO automatic fallback between providers or models: a provider that is not
configured raises ``ProviderNotConfigured`` and the caller must report BLOCKED.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ErrorKind = Literal[
    "not_configured",
    "config",
    "timeout",
    "rate_limited",
    "overloaded",
    "server",
    "connection",
    "auth",
    "bad_request",
    "not_found",
    "refusal",
    "malformed_response",
    "cancelled",
]


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


class ProviderNotConfigured(ProviderError):
    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__("not_configured", message)
        self.missing = missing


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ToolResultPart:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None => provider returned non-object / unparseable input
    raw_error: str | None = None


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: TurnUsage
    resolved_model: str | None
    provider_request_id: str | None
    latency_ms: float
    retries: int = 0
    stop_details: dict[str, Any] = field(default_factory=dict)


class ProviderSession(ABC):
    @abstractmethod
    async def send(self, parts: list[TextPart | ToolResultPart]) -> ModelTurn:
        """Append one user turn and return the model's next turn."""


class ModelProvider(ABC):
    name: str
    live: bool  # True only for real hosted-model adapters

    @abstractmethod
    def start(self, system: str, tools: list[ToolSpec]) -> ProviderSession: ...

    def parameters(self) -> dict[str, Any]:
        return {}

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
