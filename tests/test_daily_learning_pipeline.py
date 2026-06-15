from __future__ import annotations

from pathlib import Path

from omnidesk_agent.learning.daily_job import DailySelfLearningJob
from omnidesk_agent.memory.experience import ExperienceStore


def test_daily_learning_job_runs_curator_replay_drift_and_skill_pipeline(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        memory.add_experience({
            "task_type": "browser",
            "goal": "checkout",
            "success": True,
            "recommended_next_action": "reuse checkout plan",
            "risk_level": "medium",
            "reusable_skill": True,
            "success_score": 1.0,
        })
        for _ in range(2):
            memory.add_experience({
                "task_type": "browser",
                "goal": "login",
                "success": False,
                "failure_reason": "selector_changed",
                "recommended_next_action": "refresh selector map",
                "risk_level": "medium",
            })
        memory.record_metric(success=False, tool_error=True)

        report = DailySelfLearningJob(memory, tmp_path).run(days=7)

        assert report["memory_reviews"]
        assert report["replay_report"]["trace_count"] >= 1
        assert any(signal["drift_type"] == "ui_drift" for signal in report["drift_signals"])
        assert report["skill_candidates"]
        assert Path(report["report_path"]).exists()
        assert (tmp_path / "learning_audit.jsonl").exists()
