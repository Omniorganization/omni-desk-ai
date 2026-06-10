from __future__ import annotations

from omnidesk_agent.self_learning.observability.slo import IndustrialSLOEvaluator


def test_slo_missing_data_is_not_success():
    result = IndustrialSLOEvaluator().evaluate({})
    assert not result["ok"]
    assert any(v["status"] == "missing_data" for v in result["violations"])


def test_slo_flags_bad_memory_and_permission_bypass():
    result = IndustrialSLOEvaluator().evaluate({
        "task_success_rate": 0.9,
        "high_risk_misexecution_rate": 0.0,
        "permission_bypass_rate": 1.0,
        "bad_memory_rate": 0.2,
        "rollback_success_rate": 1.0,
        "test_coverage": 0.9,
        "industrial_readiness_score": 90,
    })
    assert not result["ok"]
    assert any(v["metric"] == "permission_bypass_rate" for v in result["critical_violations"])
