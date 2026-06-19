from __future__ import annotations

from pathlib import Path

from omnidesk_agent.self_learning.drift import DriftDetectionSuite
from omnidesk_agent.self_learning.replay import ReplayReportBuilder
from omnidesk_agent.self_learning.skill_learning import SkillLearningPipeline


def test_replay_report_scores_policy_improvement():
    report = ReplayReportBuilder().from_experiences([
        {
            "id": 1,
            "task_type": "browser",
            "goal": "login",
            "success": False,
            "failure_reason": "selector_changed",
            "success_score": 0.0,
        }
    ])

    assert report["trace_count"] == 1
    assert report["policy_improvement_score"] == 1
    assert report["comparisons"][0]["improved"] is True


def test_drift_detection_flags_ui_and_tool_drift():
    signals = DriftDetectionSuite().detect(
        metrics={"tool_error_rate": 0.3},
        failure_counts=[{"failure_reason": "selector_changed", "count": 3}],
        experiences=[],
    )

    drift_types = {item["drift_type"] for item in signals}
    assert "ui_drift" in drift_types
    assert "tool_failure_drift" in drift_types


def test_skill_learning_pipeline_writes_candidate_files(tmp_path):
    candidates = SkillLearningPipeline(tmp_path / "skill_candidates").run([
        {
            "id": 7,
            "task_type": "browser",
            "goal": "checkout",
            "recommended_next_action": "reuse successful checkout flow",
            "reusable_skill": True,
            "memory_status": "trusted",
            "confidence": 0.9,
        }
    ], replay_score=0.8)

    assert candidates
    assert candidates[0]["status"] == "canary"
    assert Path(candidates[0]["skill_path"]).exists()
    assert Path(candidates[0]["tests_path"]).exists()
