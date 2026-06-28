from __future__ import annotations

import pytest

from omnidesk_agent.config import ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.router import ModelRouter


def _router(tmp_path, *, routing=None) -> ModelRouter:
    cfg = ModelsConfig()
    if routing is not None:
        cfg.routing = routing
    return ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))


def test_operator_can_explicitly_select_allowed_profile(tmp_path):
    router = _router(tmp_path)

    plan = router.route_plan(
        "chat",
        {
            "profile": "planner",
            "role": "operator",
            "actor": "operator-1",
        },
    )

    assert plan.profiles == ["planner"]


def test_operator_cannot_select_high_cost_code_profile_without_approval(tmp_path):
    router = _router(tmp_path)

    with pytest.raises(PermissionError, match="high-cost model profile"):
        router.route_plan(
            "chat",
            {
                "profile": "code",
                "role": "operator",
                "actor": "operator-1",
            },
        )


def test_operator_can_select_high_cost_profile_with_explicit_approval(tmp_path):
    router = _router(tmp_path)

    plan = router.route_plan(
        "chat",
        {
            "profile": "code",
            "role": "operator",
            "actor": "operator-1",
            "approved_model_profiles": ["code"],
        },
    )

    assert plan.profiles == ["code"]


def test_tenant_profile_acl_blocks_cross_tenant_profile(tmp_path):
    routing = {
        "chat": {"primary": "fast", "fallback": ["local"], "max_retries": 1},
        "profile_acl": {
            "roles": {"operator": ["fast", "planner", "local"]},
            "tenants": {"tenant-a": ["fast", "local"]},
            "high_cost_profiles": [],
        },
    }
    router = _router(tmp_path, routing=routing)

    with pytest.raises(PermissionError, match="tenant ACL"):
        router.route_plan(
            "chat",
            {
                "profile": "planner",
                "role": "operator",
                "tenant": "tenant-a",
            },
        )
