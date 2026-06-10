from __future__ import annotations

import os
from typing import Protocol

from omnidesk_agent.config import LLMConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager


class LLM(Protocol):
    async def complete(
        self,
        system: str,
        user: str,
        *,
        task_id: str = "default",
        verified_required: bool = False,
    ) -> str:
        ...


class RuleLLM:
    async def complete(
        self,
        system: str,
        user: str,
        *,
        task_id: str = "default",
        verified_required: bool = False,
    ) -> str:
        return "rule-mode: no LLM call was made"


class OpenAIChatLLM:
    def __init__(self, cfg: LLMConfig, token_budget: TokenBudgetManager | None = None):
        self.cfg = cfg
        self.token_budget = token_budget

    async def complete(
        self,
        system: str,
        user: str,
        *,
        task_id: str = "default",
        verified_required: bool = False,
    ) -> str:
        max_output = int(getattr(self.cfg, "max_output_tokens", 1200) or 1200)

        if self.token_budget:
            decision = self.token_budget.decide(
                model=self.cfg.model,
                system=system,
                user=user,
                task_id=task_id,
                expected_output_tokens=max_output,
                verified_required=verified_required,
            )
            if not decision.allowed:
                raise RuntimeError(f"LLM call blocked by token guardrail: {decision.reason}")

            cached = self.token_budget.get_cached(decision.cache_key or "")
            if cached is not None:
                return cached

            system = decision.truncated_system or system
            user = decision.truncated_user or user

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=os.getenv(self.cfg.api_key_env))
        kwargs = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        try:
            resp = await client.chat.completions.create(**kwargs, max_completion_tokens=max_output)
        except TypeError:
            resp = await client.chat.completions.create(**kwargs, max_tokens=max_output)

        text = resp.choices[0].message.content or ""

        if self.token_budget:
            self.token_budget.record_call(
                task_id=task_id,
                model=self.cfg.model,
                estimated_input_tokens=self.token_budget.estimate_tokens(system + user),
                estimated_output_tokens=self.token_budget.estimate_tokens(text),
                verified_required=verified_required,
                budget_overridden=decision.budget_overridden,
                reason=decision.reason,
            )
            if decision.cache_key:
                self.token_budget.put_cached(
                    cache_key=decision.cache_key,
                    model=self.cfg.model,
                    response=text,
                )

        return text


# Backward-compatible name used by older runtime code.
RuleBasedLLM = RuleLLM
