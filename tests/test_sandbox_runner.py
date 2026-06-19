from __future__ import annotations

import asyncio
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def test_sandbox_runner_blocks_unlisted_command(tmp_path):
    async def run_case():
        runner = SandboxRunner(tmp_path)
        result = await runner.run("bash -c echo bad")
        assert not result.ok
        assert result.exit_code == 126

    asyncio.run(run_case())
