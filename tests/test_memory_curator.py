from __future__ import annotations

from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.self_learning.governance import MemoryCurator


def test_memory_curator_promotes_and_flags_contradictions(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        trusted_id = memory.add_experience({
            "task_type": "browser",
            "goal": "checkout",
            "success": True,
            "recommended_next_action": "reuse successful selector plan",
            "risk_level": "medium",
            "reusable_skill": True,
            "success_score": 1.0,
        })
        memory.add_experience({
            "task_type": "browser",
            "goal": "login",
            "success": True,
            "recommended_next_action": "click login button",
            "risk_level": "medium",
            "success_score": 0.9,
        })
        memory.add_experience({
            "task_type": "browser",
            "goal": "login",
            "success": False,
            "failure_reason": "selector_changed",
            "recommended_next_action": "avoid old login button",
            "risk_level": "medium",
        })

        reviews = MemoryCurator().curate_store(memory, days=7)
        by_id = {item["experience_id"]: item for item in reviews}

        assert by_id[trusted_id]["memory_status"] == "trusted"
        assert any(item["memory_status"] == "needs_review" and item["contradiction"] for item in reviews)


def test_memory_curator_blocks_security_violations(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        experience_id = memory.add_experience({
            "task_type": "shell",
            "goal": "run unsafe command",
            "success": False,
            "failure_reason": "security_violation",
            "recommended_next_action": "do not run",
            "risk_level": "critical",
        })

        reviews = MemoryCurator().curate_store(memory, days=7)

        assert reviews[0]["experience_id"] == experience_id
        assert reviews[0]["memory_status"] == "blocked"
