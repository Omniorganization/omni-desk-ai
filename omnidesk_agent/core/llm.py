from __future__ import annotations

import os
from typing import Protocol

from omnidesk_agent.config import LLMConfig


class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class RuleBasedLLM:
    async def complete(self, system: str, user: str) -> str:
        return "我会先理解任务，再选择最小权限工具执行。"


class OpenAIChatLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    async def complete(self, system: str, user: str) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("Install with `pip install -e .[llm]` to use OpenAIChatLLM") from exc
        api_key = os.getenv(self.cfg.api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing {self.cfg.api_key_env}")
        client = AsyncOpenAI(api_key=api_key, base_url=self.cfg.base_url)
        resp = await client.chat.completions.create(
            model=self.cfg.model,
            temperature=self.cfg.temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""
