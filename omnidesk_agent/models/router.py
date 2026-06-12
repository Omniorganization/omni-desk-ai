from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from omnidesk_agent.config import ModelProfileConfig, ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.base import ModelRequest, ModelResponse
from omnidesk_agent.models.providers import PROVIDER_CLASSES, ProviderSettings
from omnidesk_agent.models.cost_ledger import ModelCostLedger
from omnidesk_agent.models.provider_errors import classify_provider_error
from omnidesk_agent.models.schema_retry import StructuredOutputError, build_repair_prompt, validate_json_text


@dataclass
class RoutePlan:
    profiles: list[str]
    max_retries: int = 1
    failure_threshold: int = 5
    reset_seconds: int = 60


class ModelRouter:
    def __init__(self, cfg: ModelsConfig, token_budget: TokenBudgetManager):
        self.cfg = cfg
        self.token_budget = token_budget
        self.providers = {name: self._build_provider(name, p) for name, p in cfg.profiles.items() if p.enabled}
        self._circuit: dict[str, dict[str, float]] = {}
        self.cost_ledger = ModelCostLedger()
        self.error_counts: dict[str, int] = {}

    def _build_provider(self, name: str, p: ModelProfileConfig):
        cls = PROVIDER_CLASSES.get(p.provider)
        if not cls:
            raise ValueError(f"Unsupported model provider: {p.provider}")
        provider = cls(ProviderSettings(
            profile_name=name,
            provider=p.provider,
            model=p.model,
            api_key_env=p.api_key_env,
            base_url=p.base_url,
            api_version=p.api_version,
            region=p.region,
            max_output_tokens=p.max_output_tokens,
            temperature=p.temperature,
            extra_headers=p.extra_headers,
            extra_body=p.extra_body,
        ))
        provider.provider_name = p.provider
        return provider

    def route_plan(self, task: str, metadata: Optional[dict[str, Any]] = None) -> RoutePlan:
        metadata = metadata or {}
        explicit_profile = metadata.get("profile")
        if explicit_profile:
            return RoutePlan(profiles=[str(explicit_profile)])

        raw = self.cfg.routing.get(task) or self.cfg.routing.get("chat") or self.cfg.default
        if isinstance(raw, str):
            return RoutePlan(profiles=[raw])
        if isinstance(raw, dict):
            primary = str(raw.get("primary") or self.cfg.default)
            fallback = [str(p) for p in raw.get("fallback", []) if str(p) != primary]
            breaker = raw.get("circuit_breaker") or {}
            if not isinstance(breaker, dict):
                breaker = {}
            return RoutePlan(
                profiles=[primary] + fallback,
                max_retries=max(0, int(raw.get("max_retries", 1))),
                failure_threshold=max(1, int(breaker.get("failure_threshold", 5))),
                reset_seconds=max(1, int(breaker.get("reset_seconds", 60))),
            )
        return RoutePlan(profiles=[self.cfg.default])

    def select_profile(self, task: str) -> str:
        return self.route_plan(task).profiles[0]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        plan = self.route_plan(request.task, request.metadata)
        last_error: Optional[BaseException] = None
        attempted: list[str] = []

        for profile in plan.profiles:
            provider = self.providers.get(profile)
            if not provider:
                last_error = RuntimeError(f"Model profile not configured or disabled: {profile}")
                continue
            if self._circuit_open(profile, plan):
                last_error = RuntimeError(f"Model profile circuit open: {profile}")
                continue

            for attempt in range(plan.max_retries + 1):
                attempted.append(profile)
                try:
                    return await self._complete_with_provider(request, profile, provider)
                except Exception as exc:
                    last_error = exc
                    info = classify_provider_error(exc)
                    self.error_counts[info.category] = self.error_counts.get(info.category, 0) + 1
                    self._record_failure(profile)
                    if info.retryable and attempt < plan.max_retries:
                        await asyncio.sleep(min(0.2 * (2 ** attempt), 1.0))
                        continue
                    break

        tried = ", ".join(attempted or plan.profiles)
        raise RuntimeError(f"All model profiles failed for task={request.task}; tried={tried}; last_error={last_error}") from last_error

    async def _complete_with_provider(self, request: ModelRequest, profile: str, provider: Any) -> ModelResponse:
        decision = self.token_budget.decide(
            model=f"{profile}:{provider.model}",
            system=request.system,
            user=request.user,
            task_id=request.task_id,
            expected_output_tokens=getattr(provider.settings, "max_output_tokens", self.cfg.max_output_tokens),
            verified_required=request.verified_required,
        )
        if not decision.allowed:
            raise RuntimeError(f"Model call blocked by token guardrail: {decision.reason}")
        cached = self.token_budget.get_cached(decision.cache_key or "")
        if cached is not None:
            return ModelResponse(text=cached, provider=getattr(provider, "provider_name", "cache"), model=getattr(provider, "model", "cache"), profile=profile, usage={"cache_hit": True})
        safe = ModelRequest(
            system=decision.truncated_system or request.system,
            user=decision.truncated_user or request.user,
            task=request.task,
            images=request.images,
            json_mode=request.json_mode,
            verified_required=request.verified_required,
            task_id=request.task_id,
            metadata={**request.metadata, "profile": profile},
        )
        resp = await provider.complete(safe)
        resp = await self._repair_structured_output_if_needed(safe, profile, provider, resp)
        self._record_success(profile)
        self.token_budget.record_call(
            task_id=request.task_id,
            model=f"{profile}:{resp.model}",
            estimated_input_tokens=decision.estimated_input_tokens,
            estimated_output_tokens=self.token_budget.estimate_tokens(resp.text),
            verified_required=request.verified_required,
            budget_overridden=decision.budget_overridden,
            reason=decision.reason,
        )
        estimated_output_tokens = self.token_budget.estimate_tokens(resp.text)
        self.cost_ledger.record(
            task_id=request.task_id,
            task=request.task,
            profile=profile,
            model=resp.model,
            provider=resp.provider,
            usage={**(resp.usage or {}), "estimated_output_tokens": estimated_output_tokens},
            estimated_output_tokens=estimated_output_tokens,
        )
        if decision.cache_key:
            self.token_budget.put_cached(cache_key=decision.cache_key, model=f"{profile}:{resp.model}", response=resp.text)
        return resp

    async def _repair_structured_output_if_needed(self, request: ModelRequest, profile: str, provider: Any, resp: ModelResponse) -> ModelResponse:
        if not request.json_mode:
            return resp
        schema = request.metadata.get("json_schema") or request.metadata.get("schema")
        max_repairs = int(request.metadata.get("schema_retry_max", 1))
        try:
            validate_json_text(resp.text, schema if isinstance(schema, dict) else None)
            return resp
        except StructuredOutputError as exc:
            if max_repairs <= 0:
                raise
            system, user = build_repair_prompt(original_text=resp.text, error=str(exc), schema=schema if isinstance(schema, dict) else None)
            repair_request = ModelRequest(
                system=system,
                user=user,
                task=request.task,
                images=[],
                json_mode=True,
                verified_required=request.verified_required,
                task_id=request.task_id,
                metadata={**request.metadata, "profile": profile, "schema_retry_max": 0, "schema_repair": True},
            )
            repaired = await provider.complete(repair_request)
            validate_json_text(repaired.text, schema if isinstance(schema, dict) else None)
            return repaired

    def _circuit_open(self, profile: str, plan: RoutePlan) -> bool:
        state = self._circuit.get(profile)
        if not state:
            return False
        failures = int(state.get("failures", 0))
        opened_at = float(state.get("opened_at", 0.0))
        if failures < plan.failure_threshold:
            return False
        if time.time() - opened_at >= plan.reset_seconds:
            self._circuit.pop(profile, None)
            return False
        return True

    def _record_failure(self, profile: str) -> None:
        state = self._circuit.setdefault(profile, {"failures": 0.0, "opened_at": 0.0})
        state["failures"] = float(state.get("failures", 0.0)) + 1.0
        state["opened_at"] = time.time()

    def _record_success(self, profile: str) -> None:
        self._circuit.pop(profile, None)

    def status(self) -> dict[str, Any]:
        return {
            "default": self.cfg.default,
            "profiles": sorted(self.providers.keys()),
            "routing": self.cfg.routing,
            "circuit": self._circuit,
            "providers": {n: {"provider": p.provider_name, "model": p.model} for n, p in self.providers.items()},
            "error_counts": dict(self.error_counts),
            "cost_ledger": self.cost_ledger.summary(),
        }


def build_model_router(cfg: ModelsConfig, token_budget: TokenBudgetManager) -> ModelRouter:
    return ModelRouter(cfg, token_budget)
