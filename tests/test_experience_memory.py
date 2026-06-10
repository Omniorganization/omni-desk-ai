from omnidesk_agent.memory.experience import ExperienceStore


def test_structured_experience_retrieval_and_metrics(tmp_path):
    memory = ExperienceStore(tmp_path / "memory.sqlite3")
    memory.add_experience({
        "task_type": "browser_automation",
        "goal": "login_xiaohongshu",
        "success": False,
        "failure_reason": "captcha_required",
        "solution_attempted": ["browser.navigate"],
        "recommended_next_action": "ask_user_to_complete_captcha",
        "risk_level": "medium",
        "reusable_skill": True,
        "tags": ["xiaohongshu"],
    })
    rows = memory.retrieve_for_task("xiaohongshu captcha", limit=3)
    assert rows
    assert rows[0]["failure_reason"] == "captcha_required"

    memory.record_metric(success=False, manual_intervention=True, tool_error=True)
    report = memory.metrics_report(days=1)
    assert report["totals"]["task_count"] == 1
    assert report["manual_intervention_rate"] == 1
