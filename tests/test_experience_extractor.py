from omnidesk_agent.learning.experience_extractor import ExperienceExtractor


def test_experience_extractor_creates_structured_record():
    extractor = ExperienceExtractor()
    exp = extractor.extract(
        task="open chrome and login",
        plan={},
        run_result={
            "status": "failed",
            "goal": "login",
            "steps": [{"tool": "browser", "action": "navigate", "risk": "medium"}],
            "results": [{"ok": False, "error": "captcha required"}],
        },
    )
    assert exp["task_type"] == "browser_automation"
    assert exp["success"] is False
    assert exp["failure_reason"] == "captcha_required"
