from __future__ import annotations

from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.observability.dashboard import LearningDashboard
from omnidesk_agent.self_learning.observability.schema import LearningEvent


def test_learning_audit_log_and_dashboard_escape_html(tmp_path):
    path = tmp_path / "learning.jsonl"
    audit = LearningAuditLog(path)
    audit.append(LearningEvent(event_type="task_outcome", outcome="success", metadata={"note": "<script>alert(1)</script>"}))
    audit.append({"event_type": "memory_review", "memory_status": "validated", "confidence": 0.8})

    events = audit.read()
    assert len(events) == 2

    dashboard = LearningDashboard(audit)
    summary = dashboard.summary(days=7)
    html = dashboard.render_html(days=7)
    assert "metrics" in summary
    assert "<script>alert(1)</script>" not in html
    assert "OmniDesk Learning Observability" in html
