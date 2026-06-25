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
from omnidesk_agent.models.cost_store import ModelCostStore
from omnidesk_agent.models.budget_policy import ModelBudgetEnforcer, ModelBudgetPolicy
from omnidesk_agent.models.pricing import ModelPricingTable
from omnidesk_agent.models.provider_errors import classify_provider_error
from omnidesk_agent.models.schema_retry import StructuredOutputError, build_repair_prompt, validate_json_text


@dataclass
class RoutePlan:
    profiles: list[str]
    max_retries: int = 1
    failure_threshold: int = 5
    reset_seconds: int = 60


class ModelRouter:
    def __init__(self, cfg: ModelsConfig, token_budget: TokenBudgetManager, cost_store: ModelCostStore | None = None, *, require_persistent_ledger: bool = False):
        self.cfg = cfg
        self.token_budget = token_budget
        self.providers = {name: self._build_provider(name, p) for name, p in cfg.profiles.items() if p.enabled}
        self._circuit: dict[str, dict[str, float]] = {}
        if require_persistent_ledger and cost_store is None:
            raise RuntimeError("models.budget.require_persistent_ledger requires a configured model cost_store")
        self.cost_ledger = ModelCostLedger(store=cost_store)
        self.budget_enforcer = self._build_budget_enforcer(cost_store)
        self.pricing_table = ModelPricingTable()
        self.error_counts: dict[str, int] = {}


    def _build_budget_enforcer(self, cost_store: ModelCostStore | None) -> ModelBudgetEnforcer | None:
        budget_cfg = getattr(self.cfg, "budget", None)
        if cost_store is None or budget_cfg is None:
            return None
        policy = ModelBudgetPolicy(
            daily_usd_limit=getattr(budget_cfg, "daily_usd_limit", None),
            monthly_usd_limit=getattr(budget_cfg, "monthly_usd_limit", None),
            per_actor_daily_usd_limit=getattr(budget_cfg, "per_actor_daily_usd_limit", None),
            on_exceed=getattr(budget_cfg, "on_exceed", "require_approval"),
        )
        if policy.daily_usd_limit is None and policy.monthly_usd_limit is None and policy.per_actor_daily_usd_limit is None:
            return None
        return ModelBudgetEnforcer(cost_store, policy)

    def _check_budget(self, request: ModelRequest, *, profile: str, provider: Any) -> str | None:
        if self.budget_enforcer is None:
            return None
        projected = self._estimate_projected_cost(request, provider)
        actor = request.metadata.get("actor")
        decision = self.budget_enforcer.check(actor=str(actor) if actor else None, projected_cost_usd=projected)
        if decision.ok:
            return None
        if decision.action == "fallback_local" and profile != "local" and "local" in self.providers:
            return "fallback_local"
        raise RuntimeError(f"model budget exceeded: {decision.reason}; action={decision.action}; observed={decision.observed_cost_usd:.6f}; limit={decision.limit_usd}")

    def _estimate_projected_cost(self, request: ModelRequest, provider: Any) -> float:
        output_tokens = int(getattr(provider.settings, "max_output_tokens", self.cfg.max_output_tokens) or self.cfg.max_output_tokens)
        return self.pricing_table.estimate(
            provider=str(getattr(provider, "provider_name", "")),
            model=str(getattr(provider, "model", "")),
            input_tokens=self.token_budget.estimate_tokens(request.system) + self.token_budget.estimate_tokens(request.user),
            output_tokens=output_tokens,
        )

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

    def _offline_forbids_profile(self, profile: str, provider: Any) -> bool:
        if not getattr(self.cfg, "offline_mode", False):
            return False
        provider_name = str(getattr(provider, "provider_name", "") or getattr(getattr(provider, "settings", None), "provider", ""))
        base_url = str(getattr(getattr(provider, "settings", None), "base_url", "") or "")
        if profile == "local" and provider_name == "ollama":
            return False
        if provider_name == "ollama" and (base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")):
            return False
        return True

    def route_plan(self, task: str, metadata: Optional[dict[str, Any]] = None) -> RoutePlan:
        metadata = metadata or {}
        explicit_profile = metadata.get("profile")
        if explicit_profile:
            return RoutePlan(profiles=[str(explicit_profile)])
        if getattr(self.cfg, "offline_mode", False):
            return RoutePlan(profiles=["local"], max_retries=0)

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
            if self._offline_forbids_profile(profile, provider):
                last_error = RuntimeError(f"offline mode forbids external model profile: {profile}")
                continue
            if self._circuit_open(profile, plan):
                last_error = RuntimeError(f"Model profile circuit open: {profile}")
                continue

            budget_action = self._check_budget(request, profile=profile, provider=provider)
            if budget_action == "fallback_local":
                provider = self.providers.get("local")
                profile = "local"
                if not provider:
                    last_error = RuntimeError("model budget exceeded and local fallback is unavailable")
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
        usage = dict(resp.usage or {})
        actor = request.metadata.get("actor")
        if actor and not usage.get("actor"):
            usage["actor"] = str(actor)
        usage.setdefault("estimated_input_tokens", decision.estimated_input_tokens)
        usage.setdefault("estimated_output_tokens", estimated_output_tokens)
        if not usage.get("cost_usd") and not usage.get("estimated_cost_usd"):
            usage["estimated_cost_usd"] = self.pricing_table.estimate(
                provider=str(resp.provider),
                model=str(resp.model),
                input_tokens=decision.estimated_input_tokens,
                output_tokens=estimated_output_tokens,
            )
        self.cost_ledger.record(
            task_id=request.task_id,
            task=request.task,
            profile=profile,
            model=resp.model,
            provider=resp.provider,
            usage={**usage, "estimated_output_tokens": estimated_output_tokens},
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

    def close(self) -> None:
        close = getattr(self.cost_ledger, "close", None)
        if callable(close):
            close()

    def status(self) -> dict[str, Any]:
        return {
            "default": self.cfg.default,
            "offline_mode": bool(getattr(self.cfg, "offline_mode", False)),
            "profiles": sorted(self.providers.keys()),
            "routing": self.cfg.routing,
            "circuit": self._circuit,
            "providers": {n: {"provider": p.provider_name, "model": p.model} for n, p in self.providers.items()},
            "error_counts": dict(self.error_counts),
            "cost_ledger": self.cost_ledger.summary(),
            "cost_ledger_backend": type(self.cost_ledger.store).__name__ if self.cost_ledger.store is not None else None,
        }


def build_model_router(cfg: ModelsConfig, token_budget: TokenBudgetManager, cost_store: ModelCostStore | None = None, *, require_persistent_ledger: bool = False) -> ModelRouter:
    return ModelRouter(cfg, token_budget, cost_store, require_persistent_ledger=require_persistent_ledger)
