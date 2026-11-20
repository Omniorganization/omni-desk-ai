from __future__ import annotations

from omnidesk_agent.self_learning.drift import DriftDetectionSuite


def test_drift_detection_flags_ui_and_tool_drift():
    signals = DriftDetectionSuite().detect(
        metrics={"tool_error_rate": 0.3},
        failure_counts=[{"failure_reason": "selector_changed", "count": 3}],
        experiences=[],
    )

    drift_types = {item["drift_type"] for item in signals}
    assert "ui_drift" in drift_types
    assert "tool_failure_drift" in drift_types
