from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnidesk_agent.sandbox import runner_server
from omnidesk_agent.sandbox.runner_server import RunnerConfig


def test_web_first_container_has_its_own_network_namespace():
    unit = Path("deploy/web-first/omnidesk-web-first.service").read_text(encoding="utf-8")
    assert "--network host" not in unit
    assert "--network pasta:-T,18789" in unit  # only the gateway port reaches host loopback
    assert "--publish 127.0.0.1:13000:13000" in unit  # Web port published on loopback only
    host_script = Path("scripts/web_first_host.py").read_text(encoding="utf-8")
    # Listening on 0.0.0.0 is only safe because the unit above is namespaced.
    assert '"HOSTNAME": "0.0.0.0"' in host_script


def test_compose_socket_mounts_do_not_pretend_ro_is_isolation():
    for path in ("deploy/sandbox-runner/docker-compose.yml", "deploy/docker/docker-compose.full.yml"):
        text = Path(path).read_text(encoding="utf-8")
        assert "podman.sock:ro" not in text
        assert "OMNIDESK_SANDBOX_ALLOW_ROOTFUL_RUNTIME" in text


def _fake_info(stdout: str):
    def run(argv, **kwargs):
        assert argv[1] == "info"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return run


@pytest.mark.parametrize(
    ("runtime", "stdout", "expected"),
    [
        ("podman", json.dumps({"host": {"security": {"rootless": True}}}), True),
        ("podman", json.dumps({"host": {"security": {"rootless": False}}}), False),
        ("docker", json.dumps(["name=seccomp,profile=builtin", "name=rootless"]), True),
        ("docker", json.dumps(["name=seccomp,profile=builtin"]), False),
        ("docker", "not json", False),
    ],
)
def test_runtime_rootless_detection(monkeypatch, runtime, stdout, expected):
    monkeypatch.setattr(runner_server.subprocess, "run", _fake_info(stdout))
    assert runner_server.runtime_is_rootless(RunnerConfig(container_runtime=runtime)) is expected


def test_runner_refuses_to_start_on_rootful_runtime_unless_opted_out(monkeypatch):
    monkeypatch.setattr(runner_server.subprocess, "run", _fake_info(json.dumps(["name=seccomp"])))
    monkeypatch.delenv(runner_server.ALLOW_ROOTFUL_RUNTIME_ENV, raising=False)
    with pytest.raises(SystemExit, match="rootful"):
        runner_server.require_rootless_runtime(RunnerConfig(container_runtime="docker"))
    monkeypatch.setenv(runner_server.ALLOW_ROOTFUL_RUNTIME_ENV, "1")
    runner_server.require_rootless_runtime(RunnerConfig(container_runtime="docker"))
