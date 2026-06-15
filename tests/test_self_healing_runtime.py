from __future__ import annotations

from omnidesk_agent.self_healing import RuntimeSelfHealingController, RuntimeSignal


def test_self_healing_switches_model_after_circuit_threshold() -> None:
    decision = RuntimeSelfHealingController(failure_threshold=2).decide(
        RuntimeSignal(component="model.primary", consecutive_failures=2)
    )
    assert decision.action == "switch_model_profile"
    assert decision.autonomous is True
    assert decision.requires_human_approval is False


def test_self_healing_fails_closed_on_safety_violation() -> None:
    decision = RuntimeSelfHealingController().decide(
        RuntimeSignal(component="sandbox.remote_runner", safety_violation=True)
    )
    assert decision.action == "disable_capability"
    assert decision.autonomous is True
    assert decision.requires_human_approval is True


def test_self_healing_release_rollback_requires_human_approval() -> None:
    decision = RuntimeSelfHealingController().decide(
        RuntimeSignal(component="release.gateway", health_ok=False, rollback_ref="sha256:abc")
    )
    assert decision.action == "rollback_release"
    assert decision.autonomous is False
    assert decision.requires_human_approval is True
    assert decision.rollback_ref == "sha256:abc"
