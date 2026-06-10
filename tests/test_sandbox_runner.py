from __future__ import annotations
import pytest
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


@pytest.mark.asyncio
async def test_sandbox_runner_blocks_unlisted_command(tmp_path):
    runner = SandboxRunner(tmp_path)
    result = await runner.run("bash -c echo bad")
    assert not result.ok
    assert result.exit_code == 126
