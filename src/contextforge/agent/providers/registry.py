"""Explicit provider construction. Unknown providers are an error; there is no fallback."""

from __future__ import annotations

from contextforge.agent.providers.base import ModelProvider, ProviderError
from contextforge.agent.schemas import AgentConfig

LIVE_PROVIDERS = ("anthropic", "openai")
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5"}


def make_provider(config: AgentConfig) -> ModelProvider:
    if config.provider == "anthropic":
        from contextforge.agent.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider == "openai":
        from contextforge.agent.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(config)
    raise ProviderError(
        "config", f"unknown provider {config.provider!r}; implemented live providers: {LIVE_PROVIDERS}"
    )
