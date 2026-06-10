from __future__ import annotations
from typing import Any
from omnidesk_agent.config import ModelsConfig, ModelProfileConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.providers import PROVIDER_CLASSES, ProviderSettings

class ModelRouter:
    def __init__(self, cfg: ModelsConfig, token_budget: TokenBudgetManager):
        self.cfg=cfg; self.token_budget=token_budget
        self.providers={name:self._build_provider(name,p) for name,p in cfg.profiles.items() if p.enabled}
    def _build_provider(self, name: str, p: ModelProfileConfig):
        cls=PROVIDER_CLASSES.get(p.provider)
        if not cls: raise ValueError(f'Unsupported model provider: {p.provider}')
        provider=cls(ProviderSettings(profile_name=name, provider=p.provider, model=p.model, api_key_env=p.api_key_env, base_url=p.base_url, api_version=p.api_version, region=p.region, max_output_tokens=p.max_output_tokens, temperature=p.temperature, extra_headers=p.extra_headers, extra_body=p.extra_body))
        provider.provider_name=p.provider
        return provider
    def select_profile(self, task: str) -> str:
        return self.cfg.routing.get(task) or self.cfg.routing.get('chat') or self.cfg.default
    async def complete(self, request: ModelRequest) -> ModelResponse:
        profile=str(request.metadata.get('profile') or self.select_profile(request.task))
        provider=self.providers.get(profile)
        if not provider: raise RuntimeError(f'Model profile not configured or disabled: {profile}')
        decision=self.token_budget.decide(model=f'{profile}:{provider.model}', system=request.system, user=request.user, task_id=request.task_id, expected_output_tokens=getattr(provider.settings,'max_output_tokens',self.cfg.max_output_tokens), verified_required=request.verified_required)
        if not decision.allowed: raise RuntimeError(f'Model call blocked by token guardrail: {decision.reason}')
        cached=self.token_budget.get_cached(decision.cache_key or '')
        if cached is not None:
            return ModelResponse(text=cached, provider=getattr(provider,'provider_name','cache'), model=getattr(provider,'model','cache'), profile=profile, usage={'cache_hit':True})
        safe=ModelRequest(system=decision.truncated_system or request.system, user=decision.truncated_user or request.user, task=request.task, images=request.images, json_mode=request.json_mode, verified_required=request.verified_required, task_id=request.task_id, metadata=request.metadata)
        resp=await provider.complete(safe)
        self.token_budget.record_call(task_id=request.task_id, model=f'{profile}:{resp.model}', estimated_input_tokens=decision.estimated_input_tokens, estimated_output_tokens=self.token_budget.estimate_tokens(resp.text), verified_required=request.verified_required, budget_overridden=decision.budget_overridden, reason=decision.reason)
        if decision.cache_key: self.token_budget.put_cached(cache_key=decision.cache_key, model=f'{profile}:{resp.model}', response=resp.text)
        return resp
    def status(self)->dict[str,Any]:
        return {'default':self.cfg.default,'profiles':sorted(self.providers.keys()),'routing':self.cfg.routing,'providers':{n:{'provider':p.provider_name,'model':p.model} for n,p in self.providers.items()}}

def build_model_router(cfg: ModelsConfig, token_budget: TokenBudgetManager)->ModelRouter:
    return ModelRouter(cfg, token_budget)
