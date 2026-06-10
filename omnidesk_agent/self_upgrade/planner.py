from __future__ import annotations

from omnidesk_agent.self_upgrade.models import UpgradePlan, UpgradeRequest


class UpgradePlanner:
    """Conservative upgrade planner.

    This rule-based version is intentionally safe. A future LLM planner should
    produce the same schema and remain behind the same approval gates.
    """

    async def create_upgrade_plan(self, request: UpgradeRequest) -> UpgradePlan:
        title = request.title.strip() or "General OmniDesk AI improvement"
        lower = f"{request.title} {request.reason}".lower()
        files = ["UPGRADE_PLAN.md"]
        tests = ["python -m compileall omnidesk_agent"]
        notes = [
            "Level 4 auto-merge and auto-restart are intentionally excluded.",
            "Any Python code change must be reviewed before merge.",
        ]

        if "telegram" in lower:
            files.extend(["omnidesk_agent/channels/telegram.py", "tests/test_telegram_retry.py"])
            tests.append("pytest tests/test_telegram_retry.py")
        elif "permission" in lower or "安全" in lower:
            files.extend(["omnidesk_agent/security/permissions.py", "tests/test_permissions.py"])
            tests.append("pytest tests/test_permissions.py")
            notes.append("Permission-system changes must be treated as high risk.")
        elif "skill" in lower or "经验" in lower or "学习" in lower:
            files.extend(["omnidesk_agent/skills/registry.py", "~/.omnidesk/skills/<skill>/SKILL.md"])
        else:
            files.extend(["omnidesk_agent/core/orchestrator.py", "tests/test_self_upgrade.py"])

        return UpgradePlan(
            title=title,
            goal=request.reason or title,
            files_to_change=files,
            test_commands=tests,
            rollback_plan="Revert the `ai/*` branch or close the generated PR. Never push directly to `main`.",
            risk=request.risk,
            requires_human_approval=True,
            notes=notes,
        )
