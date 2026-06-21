from __future__ import annotations

from omnidesk_agent.models.budget_policy import ModelBudgetEnforcer, ModelBudgetPolicy
from omnidesk_agent.models.cost_store import ModelCostStore


def test_model_budget_enforcer_blocks_actor_projected_overspend(tmp_path):
    store = ModelCostStore(tmp_path / "costs.sqlite3")
    store.record(
        actor="alice",
        provider="fake",
        model="fast",
        profile="fast",
        task="chat",
        estimated_cost_usd=49.75,
    )
    enforcer = ModelBudgetEnforcer(
        store,
        ModelBudgetPolicy(
            daily_usd_limit=500.0,
            monthly_usd_limit=5000.0,
            per_actor_daily_usd_limit=50.0,
            on_exceed="block",
        ),
    )

    allowed = enforcer.check(actor="alice", projected_cost_usd=0.10)
    assert allowed.ok is True

    blocked = enforcer.check(actor="alice", projected_cost_usd=0.50)
    assert blocked.ok is False
    assert blocked.action == "block"
    assert blocked.reason == "actor model budget exceeded"
    assert blocked.limit_usd == 50.0
