"""Escape attempts against the real container sandbox must fail.

These run actual containers with the argv the product builds (plugin docker
runner, remote sandbox runner, shell docker backend) and try to break out: reach
the network, write the image or a read-only workspace, gain privileges, read the
gateway's secrets or OmniDesk's state, exhaust the host, or outlive a timeout.

They need a container runtime. Locally they are skipped when none is available;
CI sets OMNIDESK_REQUIRE_CONTAINER_TESTS=1, which turns a missing runtime or
image into a failure, so the isolation claims are exercised on every run.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE, PermissionConfig, SandboxConfig
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.sandbox import runner_server
from omnidesk_agent.sandbox.container import new_container_name, run_container
from omnidesk_agent.sandbox.isolation import runtime_state_masks
from omnidesk_agent.tools.shell import ShellTool

RUNTIME = os.environ.get("OMNIDESK_TEST_CONTAINER_RUNTIME", "docker")
REQUIRED = os.environ.get("OMNIDESK_REQUIRE_CONTAINER_TESTS", "").strip().lower() in {"1", "true", "yes"}
SECRET_VALUE = "gateway-secret-must-not-reach-the-sandbox"


def _runtime_available() -> bool:
    if shutil.which(RUNTIME) is None:
        return False
    try:
        return subprocess.run([RUNTIME, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="module")
def container_image() -> str:
    if not _runtime_available():
        if REQUIRED:
            pytest.fail(f"OMNIDESK_REQUIRE_CONTAINER_TESTS=1 but container runtime {RUNTIME!r} is not usable")
        pytest.skip(f"container runtime {RUNTIME!r} is not available")
    image = DEFAULT_SANDBOX_IMAGE
    if subprocess.run([RUNTIME, "image", "inspect", image], capture_output=True, timeout=60).returncode != 0:
        pulled = subprocess.run([RUNTIME, "pull", image], capture_output=True, timeout=600)
        if pulled.returncode != 0:
            if REQUIRED:
                pytest.fail(f"cannot pull sandbox image {image}: {pulled.stderr.decode(errors='replace')[-500:]}")
            pytest.skip(f"sandbox image {image} is not available")
    return image


PROBE = textwrap.dedent(
    r'''
    import ctypes, errno, json, os, socket, subprocess, sys
    from pathlib import Path

    def attempt(fn):
        try:
            fn()
            return "succeeded"
        except OSError as exc:
            return errno.errorcode.get(exc.errno, str(exc.errno))
        except Exception as exc:
            return type(exc).__name__

    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    libc = ctypes.CDLL(None, use_errno=True)

    def libc_call(name, *args):
        if getattr(libc, name)(*args) != 0:
            raise OSError(ctypes.get_errno(), name)

    def fork_many():
        children = []
        try:
            for _ in range(400):
                children.append(subprocess.Popen(["sleep", "5"]))
        finally:
            for child in children:
                child.kill()

    def read_cgroup(name):
        for path in (Path("/sys/fs/cgroup") / name,):
            try:
                return path.read_text().strip()
            except OSError:
                return None

    observed = {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "cap_eff": status["CapEff"].strip(),
        "no_new_privs": status["NoNewPrivs"].strip(),
        "seccomp": status["Seccomp"].strip(),
        "tcp_egress": attempt(lambda: socket.create_connection(("1.1.1.1", 53), timeout=3)),
        "dns": attempt(lambda: socket.getaddrinfo("example.com", 443)),
        # /var/tmp is world-writable in the image: only a read-only rootfs stops this.
        "write_rootfs": attempt(lambda: Path("/var/tmp/escape").write_text("x")),
        "write_etc": attempt(lambda: Path("/etc/escape").write_text("x")),
        "write_workspace": attempt(lambda: Path("/workspace/escape").write_text("x")),
        "write_tmp": attempt(lambda: Path("/tmp/scratch").write_text("x")),
        "exec_from_tmp": None,
        "setuid_root": attempt(lambda: os.setuid(0)),
        "chroot": attempt(lambda: os.chroot("/")),
        "mount_tmpfs": attempt(lambda: libc_call("mount", b"none", b"/tmp", b"tmpfs", 0, None)),
        "unshare_user_mount": attempt(lambda: libc_call("unshare", 0x10000000 | 0x00020000)),
        "fork_400": attempt(fork_many),
        "memory_max": read_cgroup("memory.max"),
        "pids_max": read_cgroup("pids.max"),
        "env_has_secret": any("gateway-secret-must-not" in v for v in os.environ.values()),
        "state_file": None,
    }
    try:
        Path("/tmp/run.sh").write_text("#!/bin/sh\necho ran\n")
        os.chmod("/tmp/run.sh", 0o755)
        observed["exec_from_tmp"] = attempt(lambda: subprocess.run(["/tmp/run.sh"], check=True, capture_output=True))
    except OSError as exc:
        observed["exec_from_tmp"] = errno.errorcode.get(exc.errno, str(exc.errno))
    state = Path("/workspace/approvals.sqlite3")
    if state.exists():
        observed["state_file"] = {
            "content": state.read_bytes()[:64].decode(errors="replace"),
            "write": attempt(lambda: state.write_text("forged")),
        }
    print(json.dumps(observed, sort_keys=True))
    '''
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "probe.py").write_text(PROBE)
    (workspace / "approvals.sqlite3").write_text("SECRET-APPROVAL-STATE")
    # A container user (65534) must be able to read the probe.
    for path in (tmp_path, workspace, workspace / "probe.py"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return workspace


def _run_probe(argv: list[str]) -> dict:
    env = {**os.environ, "OMNIDESK_ADMIN_TOKEN": SECRET_VALUE, "OMNIDESK_GATEWAY_SECRET": SECRET_VALUE}
    result = subprocess.run(argv, capture_output=True, timeout=180, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-2000:]
    return json.loads(result.stdout.decode().strip().splitlines()[-1])


def _assert_contained(observed: dict, *, memory_bytes: int) -> None:
    assert observed["uid"] == 65534 and observed["gid"] == 65534
    assert int(observed["cap_eff"], 16) == 0, "container kept capabilities"
    assert observed["no_new_privs"] == "1"
    assert observed["seccomp"] == "2", "no seccomp filter"
    assert observed["tcp_egress"] != "succeeded", "container reached the network"
    assert observed["dns"] != "succeeded", "container resolved DNS"
    assert observed["write_rootfs"] == "EROFS", "image filesystem is writable"
    assert observed["write_etc"] in {"EROFS", "EACCES"}
    assert observed["write_tmp"] == "succeeded"
    assert observed["exec_from_tmp"] != "succeeded", "tmpfs allows exec"
    assert observed["setuid_root"] == "EPERM"
    assert observed["chroot"] == "EPERM"
    assert observed["mount_tmpfs"] == "EPERM"
    assert observed["unshare_user_mount"] == "EPERM"
    assert observed["fork_400"] != "succeeded", "pids limit did not stop a fork storm"
    assert observed["pids_max"] == "128"
    assert observed["memory_max"] == str(memory_bytes)
    assert observed["env_has_secret"] is False, "gateway secrets reached the container"


def test_plugin_docker_profile_contains_an_escape_attempt(container_image, tmp_path):
    workspace = _workspace(tmp_path)
    tool = DockerPluginTool("probe", workspace / "probe.py", [], image=container_image)
    argv = tool.docker_argv(workspace / "probe.py")

    observed = _run_probe([RUNTIME, *argv[1:]])

    _assert_contained(observed, memory_bytes=256 * 1024 * 1024)
    # The plugin directory is mounted read-only at /plugin; /workspace does not exist there.
    assert observed["write_workspace"] in {"ENOENT", "EROFS"}


def test_runner_profile_contains_an_escape_attempt(container_image, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", container_image)
    cfg = runner_server.RunnerConfig(container_runtime=RUNTIME, default_image=container_image)
    argv = runner_server._build_docker_command({"argv": ["python3", "-I", "probe.py"], "readonly": True}, workspace, cfg)

    observed = _run_probe(argv)

    _assert_contained(observed, memory_bytes=512 * 1024 * 1024)
    assert observed["write_workspace"] == "EROFS"


def test_shell_docker_backend_masks_runtime_state_and_contains_escape(container_image, tmp_path):
    workspace = _workspace(tmp_path)
    shell = ShellTool(workspace, PermissionConfig(), SandboxConfig(backend="docker", docker_image=container_image))
    # The profile an allowlisted command gets (pytest: read-only workspace);
    # the probe stands in for hostile workspace code such as a conftest.py.
    argv = shell._docker_argv(["pytest"])
    image_index = argv.index(container_image)
    # Same masks the sandbox layer adds for OmniDesk's own state files.
    argv = [*argv[:image_index], *runtime_state_masks(workspace), container_image, "python3", "-I", "probe.py"]

    observed = _run_probe([RUNTIME, *argv[1:]])

    _assert_contained(observed, memory_bytes=512 * 1024 * 1024)
    assert observed["write_workspace"] == "EROFS"
    assert observed["state_file"] is not None
    assert "SECRET" not in observed["state_file"]["content"], "runtime state was readable in the sandbox"
    assert observed["state_file"]["write"] in {"EROFS", "EACCES", "EPERM"}
    assert (workspace / "approvals.sqlite3").read_text() == "SECRET-APPROVAL-STATE"


def test_a_timed_out_container_is_gone(container_image, tmp_path):
    name = new_container_name()
    argv = [RUNTIME, "run", "--rm", "--network", "none", container_image, "sleep", "120"]

    result = asyncio.run(run_container(argv, timeout=3, name=name))

    assert result.timed_out
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        listed = subprocess.run([RUNTIME, "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
        if name not in listed.stdout:
            break
        time.sleep(1)
    else:
        pytest.fail(f"container {name} still exists after its timeout")
