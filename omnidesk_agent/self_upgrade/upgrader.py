from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from omnidesk_agent.self_upgrade.models import UpgradeRequest, UpgradeRun
from omnidesk_agent.self_upgrade.patcher import UpgradePatcher
from omnidesk_agent.self_upgrade.planner import UpgradePlanner
from omnidesk_agent.self_upgrade.tester import UpgradeTester


class SelfUpgrader:
    """Level 1-3 self-upgrade workflow.

    Level 4 is deliberately not implemented: this class never merges PRs, never
    force-pushes, and never restarts the running daemon.
    """

    def __init__(self, repo_root: Path, planner: UpgradePlanner | None = None):
        self.repo_root = repo_root.resolve()
        self.planner = planner or UpgradePlanner()
        self.patcher = UpgradePatcher(self.repo_root)
        self.tester = UpgradeTester(self.repo_root)

    async def propose(self, request: UpgradeRequest, output_dir: str = ".omnidesk/upgrades") -> UpgradeRun:
        plan = await self.planner.create_upgrade_plan(request)
        patch = await self.patcher.write_plan_artifacts(plan, output_dir=output_dir)
        return UpgradeRun(request=request, plan=plan, patch=patch, status="patched")

    async def test_plan(self, run: UpgradeRun) -> UpgradeRun:
        tests = []
        for command in run.plan.test_commands:
            result = await self.tester.run(command)
            tests.append(result)
            if not result.ok:
                run.tests = tests
                run.status = "failed"
                return run
        run.tests = tests
        run.status = "tested"
        return run

    @staticmethod
    def new_upgrade_branch(prefix: str = "ai/upgrade") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{prefix}-{stamp}"
