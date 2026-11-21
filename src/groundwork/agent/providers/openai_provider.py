"""OpenAI Responses API adapter (official ``openai`` Python SDK, verified against v3.57.0).

Why the Responses API: on ``/v1/chat/completions`` gpt-5.x rejects function tools combined with
``reasoning_effort`` (observed live, HTTP 400, 2026-11-21). ``/v1/responses`` supports both.

* Credentials: ONLY an explicit ``OPENAI_API_KEY`` (or a key passed in). Workload identity, admin
  keys and provider integrations of the SDK are not used.
* Endpoint: ``base_url`` is passed explicitly (default ``https://api.openai.com/v1``) so an ambient
  ``OPENAI_BASE_URL`` cannot silently redirect an evaluation. Set ``GROUNDWORK_OPENAI_BASE_URL``
  to target a different endpoint deliberately; it is recorded in the run parameters.
* Retries: SDK retries are disabled (``max_retries=0``); this adapter retries 429/5xx/connection/
  timeout itself (bounded, counted, honouring ``retry-after``). 400/401/403/404 fail fast.
* Stateless: ``store=False``; nothing is kept provider-side and ``previous_response_id`` is never
  used. The full item history is replayed each turn, with reasoning items carried as
  ``reasoning.encrypted_content`` so reasoning survives across tool calls.
* No model fallback: the requested model id is sent as-is and the model id the API reports is
  recorded per call (the runtime warns on mismatch).
* Reasoning: ``AgentConfig.effort`` is sent as ``reasoning.effort``. Models without reasoning reject
  it with a 400, which is surfaced (never silently dropped). ``temperature`` is sent only when set.
* Stop reasons are normalised to the runtime's vocabulary: function calls->``tool_use``,
  completed->``end_turn``, incomplete(max_output_tokens)->``max_tokens``,
  incomplete(content_filter)/refusal->``refusal``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any

import openai

from groundwork.agent.providers.base import (
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
from groundwork.agent.schemas import AgentConfig

API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "GROUNDWORK_OPENAI_BASE_URL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        raw = exc.response.headers.get("retry-after")
        return float(raw) if raw is not None else None
    except (ValueError, AttributeError):
        return None


def _parse_args(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None or raw == "":
        return {}, None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"unparseable tool arguments: {exc.msg}"
    if not isinstance(val, dict):
        return None, f"non-object input: {type(val).__name__}"
    return val, None


class OpenAISession(ProviderSession):
    def __init__(self, provider: OpenAIProvider, system: str, tools: list[ToolSpec]) -> None:
        self.p = provider
        self.instructions = system
        self.tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
                "strict": False,
            }
            for t in tools
        ]
        self.items: list[dict[str, Any]] = []

    async def send(self, parts: list[TextPart | ToolResultPart]) -> ModelTurn:
        texts: list[str] = []
        for part in parts:
            if isinstance(part, ToolResultPart):
                content = part.content
                if part.is_error:
                    content = json.dumps({"is_error": True, "content": part.content})
                self.items.append(
                    {"type": "function_call_output", "call_id": part.tool_call_id, "output": content}
                )
            else:
                texts.append(part.text)
        if texts:
            self.items.append({"role": "user", "content": "\n\n".join(texts)})
        resp, latency, retries = await self.p.create_with_retries(self.instructions, self.tools, self.items)
        if resp.error is not None:
            raise ProviderError("server", f"response error: {resp.error.code}: {resp.error.message}"[:500])
        text_parts: list[str] = []
        refusal: str | None = None
        calls: list[ToolCall] = []
        for item in resp.output or []:
            # Replay every output item unchanged (reasoning incl. encrypted content, messages, calls).
            self.items.append(item.model_dump(mode="json", exclude_none=True, by_alias=True))
            kind = getattr(item, "type", None)
            if kind == "function_call":
                args, err = _parse_args(item.arguments)
                calls.append(ToolCall(id=item.call_id, name=item.name, arguments=args, raw_error=err))
            elif kind == "message":
                for c in item.content:
                    if getattr(c, "type", None) == "output_text":
                        text_parts.append(c.text)
                    elif getattr(c, "type", None) == "refusal":
                        refusal = c.refusal

        incomplete = resp.incomplete_details.reason if resp.incomplete_details else None
        stop_details: dict[str, Any] = {}
        if refusal:
            stop = "refusal"
            stop_details = {"category": "refusal", "explanation": refusal[:500]}
        elif incomplete == "content_filter":
            stop, stop_details = "refusal", {"category": "content_filter", "explanation": None}
        elif incomplete == "max_output_tokens":
            stop = "max_tokens"
        elif calls:
            stop = "tool_use"
        elif resp.status in (None, "completed"):
            stop = "end_turn"
        else:
            stop = str(incomplete or resp.status)

        u = resp.usage
        usage = TurnUsage()
        if u is not None:
            details = u.input_tokens_details
            cached = (details.cached_tokens if details else None) or 0
            written = (getattr(details, "cache_write_tokens", None) if details else None) or 0
            usage = TurnUsage(
                input_tokens=max(0, (u.input_tokens or 0) - cached - written),
                output_tokens=u.output_tokens or 0,
                cache_creation_input_tokens=written,
                cache_read_input_tokens=cached,
            )
        return ModelTurn(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            resolved_model=resp.model,
            provider_request_id=getattr(resp, "_request_id", None),
            latency_ms=latency,
            retries=retries,
            stop_details=stop_details,
        )


class OpenAIProvider(ModelProvider):
    name = "openai"
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
        if config.model.startswith("claude-"):
            raise ProviderError(
                "config", f"model {config.model!r} is not an OpenAI model; pass --model explicitly"
            )
        self.config = config
        self.base_url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        self.client = openai.AsyncOpenAI(
            api_key=key,
            base_url=self.base_url,
            max_retries=0,
            timeout=config.request_timeout_seconds,
            http_client=http_client,
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "api": "responses",
            "reasoning_effort": self.config.effort,
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_output_tokens_per_turn,
            "prompt_caching": "automatic (provider-side)",
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "base_url": self.base_url,
            "server_side_fallbacks": False,
            "sdk_version": openai.__version__,
        }

    def start(self, system: str, tools: list[ToolSpec]) -> ProviderSession:
        return OpenAISession(self, system, tools)

    async def create_with_retries(
        self, instructions: str, tools: list[dict[str, Any]], items: list[dict[str, Any]]
    ) -> tuple[Any, float, int]:
        cfg = self.config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "instructions": instructions,
            "input": items,
            "tools": tools,
            "max_output_tokens": cfg.max_output_tokens_per_turn,
            "reasoning": {"effort": cfg.effort},
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if cfg.temperature is not None:
            kwargs["temperature"] = cfg.temperature
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                resp = await self.client.responses.create(**kwargs)
                return resp, (time.perf_counter() - t0) * 1000.0, attempt
            except openai.APITimeoutError as exc:
                err = ProviderError("timeout", f"model request timed out: {exc}", retryable=True)
                delay = None
            except openai.RateLimitError as exc:
                # insufficient_quota is a 429 that will never succeed on retry.
                if getattr(exc, "code", None) == "insufficient_quota":
                    raise ProviderError("auth", "insufficient quota on the OpenAI account (429)") from exc
                err = ProviderError("rate_limited", "provider rate limit (429)", retryable=True)
                delay = _retry_after(exc)
            except openai.AuthenticationError as exc:
                raise ProviderError("auth", f"authentication failed ({exc.status_code})") from exc
            except openai.PermissionDeniedError as exc:
                raise ProviderError("auth", f"permission denied ({exc.status_code})") from exc
            except openai.NotFoundError as exc:
                raise ProviderError("not_found", f"model or endpoint not found: {cfg.model}") from exc
            except openai.BadRequestError as exc:
                raise ProviderError("bad_request", f"bad request: {exc.message}"[:500]) from exc
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    err = ProviderError(
                        "server", f"provider server error ({exc.status_code})", retryable=True
                    )
                else:
                    raise ProviderError(
                        "bad_request", f"HTTP {exc.status_code}: {exc.message}"[:500]
                    ) from exc
                delay = _retry_after(exc)
            except openai.APIConnectionError as exc:
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
