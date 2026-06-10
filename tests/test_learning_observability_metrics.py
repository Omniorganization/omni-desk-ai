from __future__ import annotations

from omnidesk_agent.self_learning.observability.metrics import LearningMetricsCalculator
from omnidesk_agent.self_learning.observability.schema import LearningEvent


def test_learning_metrics_compute_quality_and_readiness():
    events = [
        LearningEvent(event_type="task_outcome", outcome="success"),
        LearningEvent(event_type="task_outcome", outcome="success", manual_intervention=True),
        LearningEvent(event_type="task_outcome", outcome="failed"),
        LearningEvent(event_type="experience_reused", reuse_success_delta=0.2),
        LearningEvent(event_type="memory_review", memory_status="validated", confidence=0.9),
        LearningEvent(event_type="memory_review", memory_status="deprecated", stale=True),
        LearningEvent(event_type="safety_event", permission_bypass=False, high_risk_misexecution=False),
        LearningEvent(event_type="test_coverage", test_coverage=0.85),
    ]
    metrics = LearningMetricsCalculator().compute(events).to_dict()
    assert metrics["task_success_rate"] == 2 / 3
    assert metrics["bad_memory_rate"] == 0.5
    assert metrics["stale_memory_rate"] == 0.5
    assert metrics["test_coverage"] == 0.85
    assert 0 <= metrics["industrial_readiness_score"] <= 100
