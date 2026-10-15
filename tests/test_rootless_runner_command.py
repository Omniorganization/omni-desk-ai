from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from omnidesk_agent.sandbox.runner_server import RunnerConfig, _build_docker_command


def test_private_workspace_owner_maps_to_nobody_without_relaxing_isolation(tmp_path, monkeypatch):
    def info(argv, **kwargs):
        assert argv == ["podman", "info", "--format", "json"]
        assert kwargs["check"] and kwargs["timeout"] == 5
        return SimpleNamespace(stdout=json.dumps({"host": {"security": {"rootless": True}}}))

    monkeypatch.setattr("omnidesk_agent.sandbox.runner_server.subprocess.run", info)
    cfg = RunnerConfig(container_runtime="podman")
    cmd = _build_docker_command({"argv": ["python", "-m", "compileall", "."]}, tmp_path, cfg)
    for flag in ("--uidmap", "--gidmap"):
        assert [cmd[i + 1] for i, value in enumerate(cmd) if value == flag] == ["0:1:65534", "65534:0:1", "65535:65535:1"]
    assert cmd[cmd.index("--user") + 1] == "65534:65534"
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in cmd and "no-new-privileges" in cmd
    assert cmd[cmd.index("--mount") + 1].endswith("dst=/workspace,readonly")
    assert "noexec,nosuid,size=128m" in cmd[cmd.index("--tmpfs") + 1]
    assert "PYTHONPYCACHEPREFIX=/tmp/omnidesk-pycache" in cmd
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert cmd[cmd.index("--cpus") + 1] == "1.0"
    assert cmd[cmd.index("--pids-limit") + 1] == "128"


@pytest.mark.parametrize("security", [{"rootless": False}, {}, {"rootless": "true"}])
def test_podman_uid_mapping_rejects_unverified_or_rootful_runtime(tmp_path, monkeypatch, security):
    monkeypatch.setattr("omnidesk_agent.sandbox.runner_server.subprocess.run", lambda *a, **kw: SimpleNamespace(
        stdout=json.dumps({"host": {"security": security}})))
    with pytest.raises(ValueError, match="requires a rootless runtime"):
        _build_docker_command({"argv": ["python", "-m", "compileall", "."]}, tmp_path, RunnerConfig(container_runtime="podman"))
