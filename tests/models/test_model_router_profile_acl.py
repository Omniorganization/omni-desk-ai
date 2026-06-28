from __future__ import annotations

import pytest

from omnidesk_agent.config import ModelsConfig
from omnidesk_agent.core.token_budget import TokenBudgetManager
from omnidesk_agent.models.router import ModelRouter


def _router(tmp_path) -> ModelRouter:
    cfg = ModelsConfig()
    return ModelRouter(cfg, TokenBudgetManager(tmp_path / "tokens.sqlite3"))


def test_role_acl_allows_standard_profile(tmp_path):
    router = _router(tmp_path)

    plan = router.route_plan(
        "chat",
        {"profile": "planner", "role": "operator", "actor": "operator-1"},
    )

    assert plan.profiles == ["planner"]


def test_role_acl_rejects_restricted_profile(tmp_path):
    router = _router(tmp_path)

    with pytest.raises(PermissionError, match="model profile denied by ACL"):
        router.route_plan(
            "chat",
            {"profile": "code", "role": "operator", "actor": "operator-1"},
        )
