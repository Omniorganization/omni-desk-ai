from __future__ import annotations

from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def test_docker_sandbox_command_is_networkless_and_readonly(tmp_path):
    runner = SandboxRunner(tmp_path, backend="docker", docker_image="python:3.11-slim")
    cmd = runner._docker_command(["python", "-m", "compileall", "omnidesk_agent"])
    assert "--network" in cmd and "none" in cmd
    assert "--read-only" in cmd
    assert any(str(tmp_path.resolve()) in part and part.endswith(":/workspace:ro") for part in cmd)
