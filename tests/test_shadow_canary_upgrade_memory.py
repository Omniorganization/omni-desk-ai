from __future__ import annotations
from omnidesk_agent.self_upgrade.release.shadow_mode import ShadowModeEvaluator
from omnidesk_agent.self_upgrade.release.canary_release import CanaryReleaseManager
from omnidesk_agent.self_upgrade.memory.upgrade_memory import UpgradeMemory

def test_shadow_mode_recommends_canary_for_better_plan():
    result = ShadowModeEvaluator().compare_plans("task", {"steps": [{"risk": "medium"}, {"risk": "medium"}]}, {"steps": [{"risk": "low"}]})
    assert result.recommendation == "promote_to_canary"

def test_canary_only_low_risk(tmp_path):
    manager = CanaryReleaseManager(tmp_path / "canary.json")
    manager.enable("planner", "v2", allowed_risk="low")
    assert manager.should_use_canary("planner", "low")
    assert not manager.should_use_canary("planner", "high")

def test_upgrade_memory_effectiveness(tmp_path):
    memory = UpgradeMemory(tmp_path / "upgrade.sqlite3")
    memory.record({"upgrade_id": "u1", "change_type": "workflow", "target": "browser", "verdict": "effective"})
    report = memory.effectiveness("workflow")
    assert report["effective_rate"] == 1
    assert report["recommendation"] == "continue"
