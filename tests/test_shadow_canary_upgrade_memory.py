from __future__ import annotations
from omnidesk_agent.self_upgrade.release.shadow_mode import ShadowModeEvaluator
from omnidesk_agent.self_upgrade.release.canary_release import CanaryReleaseManager
from omnidesk_agent.self_upgrade.memory.upgrade_memory import UpgradeMemory
from omnidesk_agent.self_upgrade.rollback import RollbackManager, Snapshot

def test_shadow_mode_recommends_canary_for_better_plan():
    result = ShadowModeEvaluator().compare_plans("task", {"steps": [{"risk": "medium"}, {"risk": "medium"}]}, {"steps": [{"risk": "low"}]})
    assert result.recommendation == "promote_to_canary"

def test_canary_only_low_risk(tmp_path):
    manager = CanaryReleaseManager(tmp_path / "canary.json")
    manager.enable("planner", "v2", allowed_risk="low")
    assert manager.should_use_canary("planner", "low")
    assert not manager.should_use_canary("planner", "high")

def test_upgrade_memory_effectiveness(tmp_path):
    with UpgradeMemory(tmp_path / "upgrade.sqlite3") as memory:
        memory.record({"upgrade_id": "u1", "change_type": "workflow", "target": "browser", "verdict": "effective"})
        report = memory.effectiveness("workflow")
        assert report["effective_rate"] == 1
        assert report["recommendation"] == "continue"


def test_rollback_if_unhealthy_records_upgrade_memory(tmp_path):
    manager = RollbackManager(tmp_path)
    commands = []
    manager._run = lambda argv: commands.append(argv) or "ok"  # type: ignore[method-assign]
    with UpgradeMemory(tmp_path / "upgrade.sqlite3") as memory:
        result = manager.rollback_if_unhealthy(
            Snapshot(commit="abc123", branch="main"),
            upgrade_id="upgrade-1",
            memory=memory,
            health_check=lambda: False,
        )
        assert result.rolled_back is True
        assert commands == [["git", "reset", "--hard", "abc123"]]
        recent = memory.recent(1)[0]
        assert recent["rollback"] == 1
        assert recent["verdict"] == "rolled_back"
