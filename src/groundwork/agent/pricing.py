"""Documented list prices used to ESTIMATE cost from provider-reported token usage.

Built-in table: Anthropic first-party API list prices as published in the Claude API reference
(snapshot dated 2026-11-03). Prices change; re-check the provider's pricing page before relying on
an estimate, and bump ``PRICING_VERSION`` whenever this table changes. Cache writes are billed at
1.25x base input (5-minute TTL); cache reads at the listed read price.

Other providers (e.g. OpenAI) have NO built-in prices: this project does not guess them. An
operator can supply prices they have checked via a JSON file named by ``GROUNDWORK_PRICES_FILE``::

    {"source": "https://openai.com/api/pricing (checked 2026-11-20)",
     "models": {"gpt-5.5": {"input_per_mtok": 0.0, "output_per_mtok": 0.0,
                            "cache_read_per_mtok": 0.0, "cache_write_multiplier": 1.0}}}

The file's SHA-256 is folded into the pricing version recorded on every attempt. A model with no
price yields ``None`` (cost unknown), never a guessed number; a budgeted run on such a model is
BLOCKED by the harness because the budget could not be enforced.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from groundwork.agent.schemas import UsageInfo

PRICING_VERSION = "anthropic-list-2026-11-03.v1"
PRICES_FILE_ENV = "GROUNDWORK_PRICES_FILE"


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_multiplier: float = 1.25


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10),
    # claude-haiku-5-5 omitted: its cache-read price is not in the documented snapshot (cost -> unknown).
    "claude-fable-5-2": Price(8.00, 40.00, 0.20),
}


class PricingFileError(ValueError):
    pass


@lru_cache(maxsize=4)
def _operator_prices(path: str) -> tuple[dict[str, Price], str]:
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw)
        models = doc["models"]
        prices = {
            str(name): Price(
                float(p["input_per_mtok"]),
                float(p["output_per_mtok"]),
                float(p["cache_read_per_mtok"]),
                float(p.get("cache_write_multiplier", 1.0)),
            )
            for name, p in models.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PricingFileError(f"invalid {PRICES_FILE_ENV} file {path}: {exc}") from exc
    return prices, hashlib.sha256(raw).hexdigest()[:12]


def _table() -> tuple[dict[str, Price], str]:
    path = os.environ.get(PRICES_FILE_ENV)
    if not path:
        return PRICES, PRICING_VERSION
    extra, sha = _operator_prices(path)
    return PRICES | extra, f"{PRICING_VERSION}+operator:{sha}"


def pricing_version() -> str:
    return _table()[1]


def has_price(model: str) -> bool:
    return model in _table()[0]


def estimate_cost_usd(model: str, usage: UsageInfo) -> float | None:
    p = _table()[0].get(model)
    if p is None:
        return None
    return round(
        (
            usage.input_tokens * p.input_per_mtok
            + usage.cache_creation_input_tokens * p.input_per_mtok * p.cache_write_multiplier
            + usage.cache_read_input_tokens * p.cache_read_per_mtok
            + usage.output_tokens * p.output_per_mtok
        )
        / 1_000_000,
        6,
    )
