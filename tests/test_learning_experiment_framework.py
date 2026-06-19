from __future__ import annotations

from omnidesk_agent.self_learning.experiments import ExperimentManager, ExperimentObservation, ExperimentSpec


def test_learning_experiment_manager_assigns_records_and_selects_winner(tmp_path):
    manager = ExperimentManager(tmp_path / "experiments.sqlite3")
    manager.create(
        ExperimentSpec(
            experiment_id="selector-policy-v2",
            name="Selector policy V2",
            control_policy="selector-v1",
            treatment_policy="selector-v2",
            treatment_percent=50,
        )
    )

    first = manager.assign("selector-policy-v2", "task-001")
    second = manager.assign("selector-policy-v2", "task-001")
    assert first == second
    assert first.arm in {"control", "treatment"}

    for i in range(40):
        manager.record(
            ExperimentObservation(
                experiment_id="selector-policy-v2",
                unit_id=f"control-{i}",
                arm="control",
                success=i < 28,
                reward=0.7,
                cost=1.0,
                latency_ms=100,
            )
        )
        manager.record(
            ExperimentObservation(
                experiment_id="selector-policy-v2",
                unit_id=f"treatment-{i}",
                arm="treatment",
                success=i < 36,
                reward=0.9,
                cost=1.1,
                latency_ms=90,
            )
        )

    summary = manager.summary("selector-policy-v2")
    assert summary["control"]["sample_count"] == 40
    assert summary["treatment"]["success_rate"] > summary["control"]["success_rate"]

    decision = manager.select_winner("selector-policy-v2", min_samples_per_arm=30, min_success_delta=0.05)
    assert decision.winner == "treatment"
    assert decision.promote is True


def test_learning_experiment_safety_gate_blocks_treatment(tmp_path):
    manager = ExperimentManager(tmp_path / "experiments.sqlite3")
    manager.create(ExperimentSpec("unsafe-exp", "Unsafe", "old", "new", 50))
    for i in range(30):
        manager.record(ExperimentObservation("unsafe-exp", f"c-{i}", "control", True, cost=1.0))
        manager.record(ExperimentObservation("unsafe-exp", f"t-{i}", "treatment", True, cost=1.0, safety_violation=i == 0))

    decision = manager.select_winner("unsafe-exp", min_samples_per_arm=30, max_safety_violation_rate=0.0)
    assert decision.winner == "control"
    assert decision.promote is False
