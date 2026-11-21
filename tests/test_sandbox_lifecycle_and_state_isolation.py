"""Sandboxed work cannot outlive its time budget, and never sees OmniDesk's own state.

Regression tests for the 2026-11-21 security audit (medium): a timeout killed
only the docker/podman client while the container kept running, a subprocess
plugin was never killed at all (its marker file appeared after the call had
already returned), and the workspace archive sent to the remote runner carried
the approval, run and audit databases that live in workspace.root.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import tarfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool
from omnidesk_agent.sandbox import runner_server
from omnidesk_agent.sandbox.container import new_container_name, run_container, run_container_sync, with_container_name
from omnidesk_agent.sandbox.isolation import RUNTIME_STATE_NAMES, is_runtime_state, runtime_state_masks
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell script as a fake container runtime")


def _fake_runtime(tmp_path: Path) -> tuple[Path, Path]:
    """A 'docker' that runs for a long time and records kill/rm calls."""
    log = tmp_path / "runtime-calls.log"
    script = tmp_path / "fake-docker"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> {log}
        if [ "$1" = "run" ]; then
          exec sleep 30
        fi
        exit 0
        """))
    script.chmod(0o755)
    return script, log


def _ctx(tmp_path: Path) -> ToolContext:
    # Development policy that allows the high-risk plugin call; the subject here is the timeout.
    return ToolContext(permissions=PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log")))


def test_container_commands_get_a_unique_name_right_after_run():
    first, second = new_container_name(), new_container_name()
    assert first != second and first.startswith("omnidesk-sbx-")
    assert with_container_name(["docker", "run", "--rm", "img"], first)[:4] == ["docker", "run", "--name", first]
    with pytest.raises(ValueError):
        with_container_name(["docker", "exec", "x"], first)
    with pytest.raises(ValueError):
        with_container_name(["docker", "run", "--name", "other", "img"], first)


@posix_only
def test_async_timeout_kills_and_removes_the_container_by_name(tmp_path):
    runtime, log = _fake_runtime(tmp_path)
    started = time.monotonic()

    result = asyncio.run(run_container([str(runtime), "run", "--rm", "img", "sleep", "60"], timeout=0.5, name="omnidesk-sbx-test1"))

    assert result.timed_out and result.returncode == 124
    assert time.monotonic() - started < 20
    calls = log.read_text().splitlines()
    assert calls[0].startswith("run --name omnidesk-sbx-test1 ")
    assert "kill omnidesk-sbx-test1" in calls and "rm -f omnidesk-sbx-test1" in calls


@posix_only
def test_sync_timeout_kills_the_container_before_returning(tmp_path):
    runtime, log = _fake_runtime(tmp_path)

    result = run_container_sync([str(runtime), "run", "--rm", "img"], timeout=0.5, name="omnidesk-sbx-test2")

    assert result.timed_out
    assert "kill omnidesk-sbx-test2" in log.read_text().splitlines()


@posix_only
def test_docker_plugin_timeout_kills_its_container(tmp_path, monkeypatch):
    runtime, log = _fake_runtime(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text("print('{}')\n")
    tool = DockerPluginTool("slowplugin", plugin_dir / "main.py", [], timeout_seconds=1)
    original = tool.docker_argv
    monkeypatch.setattr(tool, "docker_argv", lambda entrypoint=None: [str(runtime), *original(entrypoint)[1:]])

    result = asyncio.run(tool.call("call", {"plugin_action": "x", "plugin_args": {}}, _ctx(tmp_path)))

    assert not result.ok and "timed out" in result.error
    assert any(line.startswith("kill omnidesk-sbx-") for line in log.read_text().splitlines())


@posix_only
def test_subprocess_plugin_timeout_kills_everything_it_started(tmp_path):
    """The audit's demo: the plugin returned TimeoutError at 1s and still wrote its marker at 3s."""
    marker = tmp_path / "marker"
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "main.py").write_text(textwrap.dedent(f"""\
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('alive')"])
        time.sleep(30)
        """))
    tool = SubprocessPluginTool("slowplugin", plugin_dir / "main.py", [], timeout_seconds=1)

    result = asyncio.run(tool.call("call", {"plugin_action": "x", "plugin_args": {}}, _ctx(tmp_path)))

    assert not result.ok and "timed out" in result.error
    time.sleep(3)
    assert not marker.exists()


def test_runner_timeout_kills_the_container_before_deleting_its_workspace(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "run":
            # The workspace must still exist while the container could be running.
            mount = next(a for a in argv if a.startswith("type=bind,src="))
            assert Path(mount.split("src=", 1)[1].split(",", 1)[0]).exists()
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner_server.subprocess, "run", fake_run)
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "t" * 40)
    monkeypatch.setenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", "python:test")
    monkeypatch.delenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", raising=False)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("test_x.py")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    body = json.dumps({"argv": ["pytest"], "workspace_archive_base64": base64.b64encode(buf.getvalue()).decode(), "timeout_seconds": 1}).encode()
    handler = object.__new__(runner_server.SandboxRunnerHandler)
    handler.path = "/v1/run"
    handler.server = SimpleNamespace(runner_config=runner_server.RunnerConfig(require_hmac=False, default_image="python:test"))
    handler.headers = {"content-length": str(len(body)), "authorization": "Bearer " + "t" * 40}
    handler.rfile = io.BytesIO(body)
    captured: list[tuple[int, dict]] = []
    handler._json = lambda code, data: captured.append((code, data))  # type: ignore[method-assign]

    runner_server.SandboxRunnerHandler.do_POST(handler)

    assert captured[0][1]["exit_code"] == 124
    run_call = next(c for c in calls if c[1] == "run")
    name = run_call[run_call.index("--name") + 1]
    assert ["docker", "kill", name] in calls and ["docker", "rm", "-f", name] in calls


def _workspace_with_state(root: Path) -> Path:
    root.mkdir()
    (root / "src.py").write_text("print('user code')\n")
    for name in ("approvals.sqlite3", "dual_approvals.sqlite3", "runs.sqlite3", "admin_auth_audit.jsonl", "app_sync_state.json"):
        (root / name).write_text("SECRET-STATE")
    (root / "approvals.sqlite3-wal").write_text("SECRET-STATE")
    (root / "admin_auth_audit.jsonl.key").write_text("SECRET-KEY")
    (root / "audit_checkpoints").mkdir()
    (root / "audit_checkpoints" / "audit-anchor-1.json").write_text("{}")
    (root / "pkg").mkdir()
    # Same name deeper in the tree is ordinary user data.
    (root / "pkg" / "approvals.sqlite3").write_text("fixture")
    return root


def test_runtime_state_is_never_uploaded_to_the_remote_runner(tmp_path):
    workspace = _workspace_with_state(tmp_path / "ws")

    archive = base64.b64decode(RemoteSandboxClient("http://runner.internal:18890")._archive_workspace(workspace))

    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        names = sorted(tf.getnames())
        assert names == ["pkg/approvals.sqlite3", "src.py"]
        assert all(b"SECRET" not in (tf.extractfile(m).read() if m.isfile() else b"") for m in tf.getmembers())


def test_runtime_state_masks_cover_files_side_files_and_directories(tmp_path):
    workspace = _workspace_with_state(tmp_path / "ws")

    masks = runtime_state_masks(workspace)

    pairs = list(zip(masks[::2], masks[1::2]))
    assert all(flag == "--mount" for flag, _ in pairs)
    targets = {spec for _, spec in pairs}
    assert "type=bind,src=/dev/null,dst=/workspace/approvals.sqlite3,readonly" in targets
    assert "type=bind,src=/dev/null,dst=/workspace/approvals.sqlite3-wal,readonly" in targets
    assert "type=bind,src=/dev/null,dst=/workspace/admin_auth_audit.jsonl.key,readonly" in targets
    assert "type=tmpfs,dst=/workspace/audit_checkpoints,readonly,tmpfs-size=4k" in targets
    assert not any("src.py" in spec or "/pkg" in spec for spec in targets)


@pytest.mark.parametrize(
    "name",
    [
        "RUNS.SQLITE3",  # agent-core's finding: on APFS or NTFS this opens runs.sqlite3
        "Approvals.Sqlite3-WAL",
        "AUDIT_CHEC\u212aPOINTS/anchor.json",  # KELVIN SIGN normalizes to K on APFS
        "runs.sqlite3.",  # Windows drops trailing dots and spaces
        "runs.sqlite3 ",
        "runs.sqlite3::$DATA",  # Windows: the file's own data stream
    ],
)
def test_state_is_recognised_however_a_case_insensitive_filesystem_spells_it(name):
    assert is_runtime_state(name)


def test_security_files_kept_next_to_the_audit_log_are_state_too():
    # Some deployments put the audit log in the workspace root.
    assert is_runtime_state("emergency_stop.json") and is_runtime_state("admin_sessions.sqlite3-wal")
    assert is_runtime_state("admin_sessions.sqlite3.key")


def test_folding_matches_only_the_top_level_state_entries():
    assert not is_runtime_state("pkg/RUNS.SQLITE3")
    assert not is_runtime_state("runs.sqlite3.bak")
    assert not is_runtime_state("my_runs.sqlite3")


def test_every_sqlite_store_name_the_runtime_creates_is_classified_as_state():
    source = Path("omnidesk_agent/repositories/sqlite.py").read_text(encoding="utf-8")
    import re

    created = set(re.findall(r'workspace_root / "([^"]+)"', source))
    assert created, "expected the SQLite repository factory to create stores under workspace_root"
    assert created <= RUNTIME_STATE_NAMES, sorted(created - RUNTIME_STATE_NAMES)
    assert is_runtime_state("approvals.sqlite3-shm") and not is_runtime_state("pkg/approvals.sqlite3")
