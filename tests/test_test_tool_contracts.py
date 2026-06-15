from __future__ import annotations

from dataclasses import dataclass

import pytest

from omnidesk_agent.security.permissions import PermissionDecision
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.test_tool import TestTool


class AllowPermissions:
    def verify(self, proposal):
        return PermissionDecision(True, "allow", "ok")


class DryRunPermissions:
    def verify(self, proposal):
        return PermissionDecision(False, "dry_run", "dry")


def ctx(perms=None):
    return ToolContext(permissions=perms or AllowPermissions(), source="test", actor="u")


@pytest.mark.asyncio
async def test_test_tool_contracts_do_not_spawn_real_process(monkeypatch, tmp_path):
    tool = TestTool(tmp_path)
    assert not (await tool.call("bad", {}, ctx())).ok
    assert not (await tool.call("run", {"command": "sudo reboot"}, ctx())).ok
    assert not (await tool.call("run", {"command": "pytest"}, ctx(DryRunPermissions()))).ok

    @dataclass
    class Result:
        ok: bool = True
        output: str = "passed"
        command: str = "pytest"
        exit_code: int = 0

    async def fake_run(command):
        return Result(command=command)

    monkeypatch.setattr(tool.tester, "run", fake_run)
    result = await tool.call("run", {"command": "pytest"}, ctx())
    assert result.ok
