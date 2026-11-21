from __future__ import annotations

import asyncio

from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def test_docker_sandbox_command_is_networkless_and_readonly(tmp_path):
    runner = SandboxRunner(tmp_path, backend="docker")
    cmd = runner._docker_command(["python", "-m", "compileall", "omnidesk_agent"])
    assert "--network" in cmd and "none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop" in cmd and "ALL" in cmd
    assert "--security-opt" in cmd and "no-new-privileges" in cmd
    tmpfs = cmd[cmd.index("--tmpfs") + 1]
    assert tmpfs.startswith("/tmp:rw,")
    assert "noexec" in tmpfs and "nosuid" in tmpfs
    assert "--pids-limit" in cmd and "128" in cmd
    assert "--user" in cmd and "65534:65534" in cmd
    assert DEFAULT_SANDBOX_IMAGE in cmd
    assert any(str(tmp_path.resolve()) in part and "dst=/workspace" in part and "readonly" in part for part in cmd)


def test_docker_backend_executes_docker_command(tmp_path, monkeypatch):
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self, input=None):
            return b"ok", b""

        def kill(self):
            captured["killed"] = True

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return Proc()

    async def run_case():
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        runner = SandboxRunner(tmp_path, backend="docker")
        result = await runner.run(["python", "-m", "compileall", "."])
        assert result.ok is True
        assert result.command.startswith("docker run --rm")
        # The launcher names the container so a timeout can kill it.
        assert captured["argv"][:3] == ["docker", "run", "--name"] and captured["argv"][3].startswith("omnidesk-sbx-")
        assert captured["argv"][4:7] == ["--rm", "--network", "none"]
        assert "--read-only" in captured["argv"]
        assert any(str(tmp_path.resolve()) in part and "dst=/workspace" in part for part in captured["argv"])

    asyncio.run(run_case())
