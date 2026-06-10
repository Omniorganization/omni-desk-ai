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
