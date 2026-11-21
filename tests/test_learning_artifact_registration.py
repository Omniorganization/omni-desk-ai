"""The daily job may only register learned skills as candidates, and a skill
shared with every requester must not carry any requester's message text."""
from __future__ import annotations

import json

import pytest

from omnidesk_agent.learning.daily_job import DailySelfLearningJob
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.self_learning import cli
from omnidesk_agent.self_learning.skill_learning.structural_miner import StructuralSkillMiner

SECRET_GOAL = "reply to customer Zhang Wei about order 8812 refund"


def _success(memory, actor, *, goal=SECRET_GOAL, sequence=("browser.open", "browser.extract")):
    return memory.add_experience({
        "task_type": "browser_automation",
        "goal": goal,
        "success": True,
        "solution_attempted": list(sequence),
        "recommended_next_action": "reuse successful plan",
        "risk_level": "medium",
        "reusable_skill": True,
        "success_score": 1.0,
        "memory_status": "validated",
    }, channel="telegram", actor=actor)


def test_daily_job_registers_mined_skills_as_candidates_only(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        for actor in ("alice", "bob", "carol"):
            _success(memory, actor)
        job = DailySelfLearningJob(memory, tmp_path)
        report = job.run(days=7)

    learned = report["learned_artifacts"]
    assert learned["applied_by_this_job"] == 0
    assert [item["status"] for item in learned["registered"]] == ["candidate"]
    artifact = job.policy_registry.get(learned["registered"][0]["artifact_id"])
    assert "browser.open -> browser.extract" in artifact.body
    assert "Zhang Wei" not in artifact.body and "8812" not in artifact.body
    # Registration does not change what the planner sees.
    assert job.policy_registry.active_policy().is_empty


def test_one_requesters_habit_is_not_generalized():
    rows = [{"id": i, "success": True, "task_type": "general", "solution_attempted": ["files.read", "files.write"],
             "channel": "telegram", "actor": "alice"} for i in range(5)]
    assert StructuralSkillMiner().mine(rows) == []
    rows.append({**rows[0], "id": 9, "actor": "bob"})
    assert len(StructuralSkillMiner().mine(rows)) == 1


def test_private_or_malformed_sequences_are_ignored():
    base = {"success": True, "task_type": "general", "channel": "c"}
    rows = [{**base, "id": i, "actor": f"a{i}", "solution_attempted": ["files.read", "files.write"], "privacy_level": "sensitive"} for i in range(4)]
    rows += [{**base, "id": 10 + i, "actor": f"b{i}", "solution_attempted": ["files.read", "write `rm -rf`"]} for i in range(4)]
    assert StructuralSkillMiner().mine(rows) == []


def test_cli_drives_the_lifecycle_with_a_human_identity(tmp_path, capsys):
    with cli.open_registry(tmp_path) as registry:
        artifact = registry.register_candidate("workflow", "check-stock", "Check stock before quoting a price.")
        registry.record_evaluation(artifact.artifact_id, {"verdict": "non_inferior", "method": "paired_replay_v1", "trace_count": 4})

    approval = cli.run(["request-approval", artifact.artifact_id], workspace_root=tmp_path)
    with pytest.raises(PermissionError, match="human identity"):
        cli.run(["decide", approval["approval_id"], "approved", "--reviewer", "system"], workspace_root=tmp_path)
    decided = cli.run(["decide", approval["approval_id"], "approved", "--reviewer", "owner@example.com"], workspace_root=tmp_path)
    assert decided["status"] == "approved"

    policy = cli.run(["apply", artifact.artifact_id, "--actor", "owner@example.com"], workspace_root=tmp_path)
    assert policy["rules"] == ["Check stock before quoting a price."]
    assert cli.run(["active"], workspace_root=tmp_path)["version"] == policy["version"]

    assert cli.main(["--workspace", str(tmp_path), "rollback", "--actor", "owner@example.com", "--reason", "drill"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"] == []
    history = cli.run(["history"], workspace_root=tmp_path)["history"]
    assert [h["action"] for h in history] == ["rollback", "apply"]
    assert history[0]["actor"].startswith("owner@example.com (os-user:")
    assert cli.main(["--workspace", str(tmp_path), "apply", artifact.artifact_id, "--actor", "owner@example.com"]) == 2
