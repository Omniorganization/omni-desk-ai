from __future__ import annotations
from typing import Protocol
from omnidesk_agent.config import LLMConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.router import ModelRouter, build_model_router

class LLM(Protocol):
    async def complete(self, system: str, user: str, *, task_id: str='default', verified_required: bool=False) -> str: ...

class RuleLLM:
    async def complete(self, system: str, user: str, *, task_id: str='default', verified_required: bool=False) -> str:
        return 'rule-mode: no LLM call was made'

class RouterLLMAdapter:
    def __init__(self, router: ModelRouter, task: str='chat'):
        self.router=router; self.task=task
    async def complete(self, system: str, user: str, *, task_id: str='default', verified_required: bool=False) -> str:
        resp=await self.router.complete(ModelRequest(system=system,user=user,task=self.task,task_id=task_id,verified_required=verified_required)) # type: ignore[arg-type]
        return resp.text

class OpenAIChatLLM(RouterLLMAdapter):
    def __init__(self, cfg: LLMConfig, token_budget: TokenBudgetManager | None=None):
        from omnidesk_agent.config import ModelsConfig, ModelProfileConfig
        if token_budget is None: raise RuntimeError('OpenAIChatLLM now requires TokenBudgetManager through ModelRouter')
        mc=ModelsConfig(default='compat_openai',profiles={'compat_openai':ModelProfileConfig(provider='openai',model=cfg.model,api_key_env=cfg.api_key_env,base_url=cfg.base_url,temperature=cfg.temperature,max_output_tokens=cfg.max_output_tokens)},routing={'chat':'compat_openai'})
        super().__init__(build_model_router(mc, token_budget), task='chat')

RuleBasedLLM=RuleLLM
LLMClient=LLM
