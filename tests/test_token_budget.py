from __future__ import annotations
from pathlib import Path
from omnidesk_agent.core.token_budget import TokenBudgetManager, TokenBudgetConfig


def test_verified_required_overrides_budget(tmp_path: Path):
    mgr = TokenBudgetManager(tmp_path / "token.sqlite3", TokenBudgetConfig(require_approval_above_estimated_tokens=10))
    decision = mgr.decide(
        model="test",
        system="x" * 100,
        user="y" * 100,
        expected_output_tokens=100,
        verified_required=True,
    )
    assert decision.allowed
    assert decision.budget_overridden


def test_per_task_llm_call_limit_is_hard_stop(tmp_path: Path):
    mgr = TokenBudgetManager(
        tmp_path / "token.sqlite3",
        TokenBudgetConfig(require_approval_above_estimated_tokens=10, per_task_max_llm_calls=1),
    )
    allowed = mgr.decide(model="test", system="s", user="u", task_id="task-1", verified_required=True)
    assert allowed.allowed
    mgr.record_call(
        task_id="task-1",
        model="test",
        estimated_input_tokens=allowed.estimated_input_tokens,
        estimated_output_tokens=allowed.estimated_output_tokens,
        verified_required=True,
        budget_overridden=allowed.budget_overridden,
        reason=allowed.reason,
    )

    blocked = mgr.decide(model="test", system="s", user="u", task_id="task-1", verified_required=True)
    assert blocked.allowed is False
    assert "per-task model call limit exceeded" in blocked.reason


def test_token_estimate_counts_cjk_chars_without_latin_undercount():
    assert TokenBudgetManager.estimate_tokens("abcd" * 3) == 4
    assert TokenBudgetManager.estimate_tokens("汉" * 1000) >= 1000
