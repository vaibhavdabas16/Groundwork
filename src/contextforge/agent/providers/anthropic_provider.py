"""Anthropic Messages API adapter (official ``anthropic`` Python SDK, verified against v2.11.0).

* Credentials: ONLY an explicit ``ANTHROPIC_API_KEY`` (or a key passed in). The SDK's implicit
  credential discovery (CLI profiles, workload identity) is deliberately not used, so a run can
  never silently bill an account the operator did not configure for this project.
* Retries: SDK retries are disabled (``max_retries=0``); this adapter performs bounded retries
  itself so every retry is counted, honours ``retry-after`` and respects the caller's deadline.
* Sampling: SDK 2.x sends API version 2026-10-21, which rejects ``temperature``/``top_p``/``top_k``
  on every model (``messages.create`` no longer accepts them). A configured temperature is
  therefore a configuration error, not something silently dropped.
* No server-side model fallbacks are requested: an evaluation must never switch models silently.
  A refusal is surfaced as ``ProviderError("refusal")``.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

import anthropic

from contextforge.agent.providers.base import (
    ModelProvider,
    ModelTurn,
    ProviderError,
    ProviderNotConfigured,
    ProviderSession,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolSpec,
    TurnUsage,
)
from contextforge.agent.schemas import AgentConfig

API_KEY_ENV = "ANTHROPIC_API_KEY"


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        raw = exc.response.headers.get("retry-after")
        return float(raw) if raw is not None else None
    except (ValueError, AttributeError):
        return None


class AnthropicSession(ProviderSession):
    def __init__(self, provider: AnthropicProvider, system: str, tools: list[ToolSpec]) -> None:
        self.p = provider
        self.system = system
        self.tools: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
        ]
        self.messages: list[dict[str, Any]] = []

    async def send(self, parts: list[TextPart | ToolResultPart]) -> ModelTurn:
        content: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, ToolResultPart):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.tool_call_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
            else:
                content.append({"type": "text", "text": part.text})
        self.messages.append({"role": "user", "content": content})
        resp, latency, retries = await self.p.create_with_retries(self.system, self.tools, self.messages)
        # Append the provider's content blocks unchanged (thinking blocks must be replayed as-is).
        self.messages.append({"role": "assistant", "content": resp.content})
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else None
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=args,
                        raw_error=None
                        if args is not None
                        else f"non-object input: {type(block.input).__name__}",
                    )
                )
        u = resp.usage
        stop_details: dict[str, Any] = {}
        sd = getattr(resp, "stop_details", None)
        if sd is not None:
            stop_details = {
                "category": getattr(sd, "category", None),
                "explanation": getattr(sd, "explanation", None),
            }
        return ModelTurn(
            text="\n".join(text_parts),
            tool_calls=calls,
            stop_reason=str(resp.stop_reason),
            usage=TurnUsage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
                cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            ),
            resolved_model=resp.model,
            provider_request_id=getattr(resp, "_request_id", None),
            latency_ms=latency,
            retries=retries,
            stop_details=stop_details,
        )


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    live = True

    def __init__(
        self,
        config: AgentConfig,
        *,
        api_key: str | None = None,
        http_client: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get(API_KEY_ENV)
        if not key:
            raise ProviderNotConfigured(
                f"{API_KEY_ENV} is not set; live model calls are BLOCKED (no implicit credentials are used).",
                missing=[API_KEY_ENV],
            )
        if config.temperature is not None:
            raise ProviderError(
                "config",
                "temperature is not supported: the installed Anthropic SDK sends API version 2026-10-21, "
                "which rejects sampling parameters on every model. Unset temperature.",
            )
        self.config = config
        self.client = anthropic.AsyncAnthropic(
            api_key=key,
            max_retries=0,
            timeout=config.request_timeout_seconds,
            http_client=http_client,
            base_url=base_url,
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "effort": self.config.effort,
            "thinking": "adaptive",
            "max_tokens": self.config.max_output_tokens_per_turn,
            "prompt_caching": self.config.prompt_caching,
            "server_side_fallbacks": False,
            "sdk_version": anthropic.__version__,
        }

    def start(self, system: str, tools: list[ToolSpec]) -> ProviderSession:
        return AnthropicSession(self, system, tools)

    async def create_with_retries(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> tuple[Any, float, int]:
        cfg = self.config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens_per_turn,
            "system": system,
            "tools": tools,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": cfg.effort},
        }
        if cfg.prompt_caching:
            kwargs["cache_control"] = {"type": "ephemeral"}
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                resp = await self.client.messages.create(**kwargs)
                return resp, (time.perf_counter() - t0) * 1000.0, attempt
            except anthropic.APITimeoutError as exc:
                err = ProviderError("timeout", f"model request timed out: {exc}", retryable=True)
                delay = None
            except anthropic.RateLimitError as exc:
                err = ProviderError("rate_limited", "provider rate limit (429)", retryable=True)
                delay = _retry_after(exc)
            except anthropic.AuthenticationError as exc:
                raise ProviderError("auth", f"authentication failed ({exc.status_code})") from exc
            except anthropic.PermissionDeniedError as exc:
                raise ProviderError("auth", f"permission denied ({exc.status_code})") from exc
            except anthropic.NotFoundError as exc:
                raise ProviderError("not_found", f"model or endpoint not found: {cfg.model}") from exc
            except anthropic.BadRequestError as exc:
                raise ProviderError("bad_request", f"bad request: {exc.message}"[:500]) from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code == 529:
                    err = ProviderError("overloaded", "provider overloaded (529)", retryable=True)
                elif exc.status_code >= 500:
                    err = ProviderError(
                        "server", f"provider server error ({exc.status_code})", retryable=True
                    )
                else:
                    raise ProviderError(
                        "bad_request", f"HTTP {exc.status_code}: {exc.message}"[:500]
                    ) from exc
                delay = _retry_after(exc)
            except anthropic.APIConnectionError as exc:
                err = ProviderError("connection", f"connection error: {exc}", retryable=True)
                delay = None
            if attempt >= cfg.max_provider_retries:
                raise err
            attempt += 1
            jitter = random.uniform(0, 1)  # noqa: S311 - backoff jitter, not crypto
            backoff = delay if delay is not None else min(30.0, 1.0 * 2**attempt + jitter)
            await asyncio.sleep(min(backoff, 60.0))

    async def aclose(self) -> None:
        await self.client.close()
