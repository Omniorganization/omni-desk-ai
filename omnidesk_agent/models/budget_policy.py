from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from omnidesk_agent.models.cost_store import ModelCostStore

BudgetAction = Literal["allow", "require_approval", "fallback_local", "block"]


@dataclass(frozen=True)
class ModelBudgetPolicy:
    daily_usd_limit: float | None = None
    monthly_usd_limit: float | None = None
    per_actor_daily_usd_limit: float | None = None
    on_exceed: BudgetAction = "require_approval"


@dataclass(frozen=True)
class BudgetDecision:
    ok: bool
    action: BudgetAction
    reason: str
    observed_cost_usd: float
    limit_usd: float | None


class ModelBudgetEnforcer:
    def __init__(self, store: ModelCostStore, policy: ModelBudgetPolicy):
        self.store = store
        self.policy = policy

    def check(self, *, actor: str | None = None, projected_cost_usd: float = 0.0) -> BudgetDecision:
        if self.policy.daily_usd_limit is not None:
            summary = self.store.summary(days=1)
            observed = float(summary.get("estimated_cost_usd", 0.0)) + projected_cost_usd
            if observed > self.policy.daily_usd_limit:
                return BudgetDecision(False, self.policy.on_exceed, "daily model budget exceeded", observed, self.policy.daily_usd_limit)
        if self.policy.monthly_usd_limit is not None:
            summary = self.store.summary(days=31)
            observed = float(summary.get("estimated_cost_usd", 0.0)) + projected_cost_usd
            if observed > self.policy.monthly_usd_limit:
                return BudgetDecision(False, self.policy.on_exceed, "monthly model budget exceeded", observed, self.policy.monthly_usd_limit)
        if actor and self.policy.per_actor_daily_usd_limit is not None:
            summary = self.store.summary(days=1, group_by="actor")
            observed = float(summary.get("groups", {}).get(actor, {}).get("estimated_cost_usd", 0.0)) + projected_cost_usd
            if observed > self.policy.per_actor_daily_usd_limit:
                return BudgetDecision(False, self.policy.on_exceed, "actor model budget exceeded", observed, self.policy.per_actor_daily_usd_limit)
        return BudgetDecision(True, "allow", "within budget", projected_cost_usd, None)
