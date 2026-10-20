from __future__ import annotations

from pathlib import Path

from omnidesk_agent.self_learning.schemas import LearningProposal
from omnidesk_agent.self_learning.validator import SandboxValidator


def _proposal(test_plan: list[str]) -> LearningProposal:
    return LearningProposal(
        stage="draft",
        proposal_type="code_fix",
        title="t",
        problem="p",
        proposed_change="c",
        expected_benefit="b",
        test_plan=test_plan,
        proposal_id="prop-1",
    )


def test_validator_blocks_commands_outside_the_sandbox_allowlist(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    result = SandboxValidator(tmp_path).validate(_proposal([f"touch {marker}"]))
    assert result.ok is False
    assert result.command_results[0]["exit_code"] == 126
    assert not marker.exists()


def test_validator_runs_allowlisted_commands(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    result = SandboxValidator(tmp_path).validate(_proposal(["python3 -m compileall -q ok.py"]))
    assert result.ok is True, result.command_results
