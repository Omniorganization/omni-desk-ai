from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ModelPrice:
    input_per_million_usd: float
    output_per_million_usd: float


class ModelPricingTable:
    """Server-side price table for budget projection and ledger fallback.

    Provider responses may still report exact billed cost, but pre-call budget
    checks must not trust caller-supplied metadata for projected spend.
    """

    def __init__(self, prices: Mapping[tuple[str, str], ModelPrice] | None = None):
        self._prices = dict(DEFAULT_PRICES)
        if prices:
            self._prices.update(prices)

    def estimate(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        price = self._prices.get((provider.lower(), model.lower()))
        if price is None:
            price = self._prices.get((provider.lower(), "*"))
        if price is None:
            return 0.0
        input_cost = max(0, int(input_tokens)) * price.input_per_million_usd / 1_000_000
        output_cost = max(0, int(output_tokens)) * price.output_per_million_usd / 1_000_000
        return input_cost + output_cost


DEFAULT_PRICES: dict[tuple[str, str], ModelPrice] = {
    ("openai", "gpt-5.1"): ModelPrice(input_per_million_usd=1.25, output_per_million_usd=10.00),
    ("openai", "gpt-5.1-mini"): ModelPrice(input_per_million_usd=0.25, output_per_million_usd=2.00),
    ("openai", "gpt-5"): ModelPrice(input_per_million_usd=1.25, output_per_million_usd=10.00),
    ("openai", "gpt-5-mini"): ModelPrice(input_per_million_usd=0.25, output_per_million_usd=2.00),
    ("openai", "gpt-4.1"): ModelPrice(input_per_million_usd=2.00, output_per_million_usd=8.00),
    ("openai", "gpt-4.1-mini"): ModelPrice(input_per_million_usd=0.40, output_per_million_usd=1.60),
    ("openai", "gpt-4o"): ModelPrice(input_per_million_usd=2.50, output_per_million_usd=10.00),
    ("openai", "gpt-4o-mini"): ModelPrice(input_per_million_usd=0.15, output_per_million_usd=0.60),
    # Non-OpenAI routed production profiles use explicit non-zero fallback
    # estimates so budget enforcement and ledgers do not silently record zero
    # when OpenAI-compatible providers omit `cost_usd` in the response usage.
    ("deepseek", "deepseek-v4-pro"): ModelPrice(input_per_million_usd=1.00, output_per_million_usd=4.00),
    ("deepseek", "*"): ModelPrice(input_per_million_usd=1.00, output_per_million_usd=4.00),
    ("dashscope", "qwen-plus"): ModelPrice(input_per_million_usd=0.50, output_per_million_usd=2.00),
    ("dashscope", "*"): ModelPrice(input_per_million_usd=0.50, output_per_million_usd=2.00),
    ("qwen", "*"): ModelPrice(input_per_million_usd=0.50, output_per_million_usd=2.00),
    ("ollama", "*"): ModelPrice(input_per_million_usd=0.0, output_per_million_usd=0.0),
    ("local", "*"): ModelPrice(input_per_million_usd=0.0, output_per_million_usd=0.0),
}
