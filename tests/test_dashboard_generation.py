from __future__ import annotations
from omnidesk_agent.self_upgrade.dashboard.upgrade_dashboard import build_dashboard_html

def test_dashboard_html_contains_upgrade_queue():
    html = build_dashboard_html({"proposals": [{"proposal_id": "p1", "title": "T", "score": 0.5, "risk_level": "low", "status": "pending", "upgrade_type": "prompt"}]})
    assert "Upgrade Queue" in html
    assert "p1" in html
