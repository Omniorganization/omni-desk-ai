from omnidesk_agent.self_upgrade.skill_sandbox import SkillSandbox


def test_skill_sandbox_creates_skill(tmp_path):
    sandbox = SkillSandbox(tmp_path)
    path = sandbox.create_skill("gmail_reply_skill", "# Gmail Reply Skill")
    assert path.exists()
    assert path.name == "SKILL.md"
