"""Plugin low-severity findings from the second review.

- sign_plugin.py moved a v1 manifest to v2 without asking, signing whatever
  unsigned fields and files had been changed since the v1 signature;
- the signature was checked only at load, execution used the live directory,
  and __pycache__ was skipped by the signature (each call now runs a freshly
  verified copy of the directory resolved at load);
- plugin call approvals did not include the arguments (see also
  test_tool_approval_content_binding.py);
- a plugin could take the name of a built-in tool that is disabled, or one
  that differs only in case.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnidesk_agent.config import AppConfig, PluginConfig, SandboxConfig
from omnidesk_agent.core.models import ApprovalDecision
from omnidesk_agent.plugins.docker_runner import DockerPluginTool, RemoteDockerPluginTool
from omnidesk_agent.plugins.manifest import PluginManifest
from omnidesk_agent.plugins.registry import RESERVED_TOOL_NAMES, PluginRegistry
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient, RemoteSandboxResult
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.test_tool import TestTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool
from omnidesk_agent.tools.vision import VisionGroundingTool
from omnidesk_agent.validation.production import REQUIRE_PRODUCTION_GUARDS_ENV

SECRET = "secret"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _non_production_env(monkeypatch):
    for name in ("OMNIDESK_ENV", "APP_ENV", "ENV", "KUBERNETES_SERVICE_HOST", REQUIRE_PRODUCTION_GUARDS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", SECRET)


class Allow:
    def __init__(self) -> None:
        self.proposals: list[Any] = []

    def verify(self, action_proposal: Any) -> ApprovalDecision:
        self.proposals.append(action_proposal)
        return ApprovalDecision(True, "allow", "test")


def _ctx(permissions: Any) -> ToolContext:
    return ToolContext(permissions=permissions, source="web", actor="owner", run_id="r", plan_id="p", step_index=0)


def _v2_plugin(root: Path, name: str, *, sandbox: str = "subprocess") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("import helper\n\ndef call(action, args):\n    return {}\n", encoding="utf-8")
    (plugin_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = PluginManifest(name=name, trusted=True, sandbox=sandbox, entrypoint="plugin.py", permissions=["plugin.call"])
    manifest.sign_v2(plugin_dir, SECRET)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest.model_dump(exclude_none=True)), encoding="utf-8")
    return plugin_dir


def _v1_plugin(root: Path, name: str = "legacy") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    entry = plugin_dir / "plugin.py"
    entry.write_text("def call(action, args):\n    return {}\n", encoding="utf-8")
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    manifest = {
        "name": name,
        "trusted": True,
        "sandbox": "docker",
        "permissions": ["network.write"],
        "sha256": digest,
        "signature": hmac.new(SECRET.encode(), digest.encode(), hashlib.sha256).hexdigest(),
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (plugin_dir / "helper.py").write_text("UNSIGNED = True\n", encoding="utf-8")
    return plugin_dir


def _load(root: Path, app_config: Any = None, **cfg: Any) -> tuple[PluginRegistry, ToolRegistry]:
    registry = PluginRegistry(root, PluginConfig(**cfg))
    tools = ToolRegistry()
    registry.load_into(tools, app_config if app_config is not None else AppConfig())
    return registry, tools


class _Spawns:
    """Records create_subprocess_exec calls instead of running anything."""

    def __init__(self, on_spawn: Any = None) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.on_spawn = on_spawn

    async def __call__(self, *argv: str, **kwargs: Any):
        self.argv.append(argv)
        if self.on_spawn is not None:
            self.on_spawn(argv, kwargs)

        class Proc:
            returncode = 0

            async def communicate(self, data: bytes):
                return b"{}", b""

        return Proc()


# --- names -------------------------------------------------------------------

def test_every_builtin_tool_name_is_reserved():
    builtins = [FilesTool, GitTool, TestTool, ShellTool, ComputerTool, VisionGroundingTool, PullRequestTool, ChannelSendTool, UIBridgeTool, BrowserTool, GmailTool]
    assert {tool.name for tool in builtins} <= RESERVED_TOOL_NAMES


@pytest.mark.parametrize("name", ["shell", "Shell", "GMAIL", "pull-request", "Pull_Request", "ui.bridge", "Channels", "plugins"])
def test_plugin_cannot_take_a_builtin_name_even_when_the_tool_is_disabled(tmp_path, name):
    _v2_plugin(tmp_path, name)
    tools = ToolRegistry()  # no built-in tool registered at all
    with pytest.raises(PermissionError, match="reserved for a built-in tool"):
        PluginRegistry(tmp_path, PluginConfig()).load_into(tools, AppConfig())
    assert tools.names() == []


def test_plugin_name_collisions_ignore_case(tmp_path):
    _v2_plugin(tmp_path, "deploy")
    tools = ToolRegistry()
    tools.register(type("Other", (), {"name": "Deploy"})())
    with pytest.raises(PermissionError, match="already registered: deploy"):
        PluginRegistry(tmp_path, PluginConfig()).load_into(tools, AppConfig())

    other = tmp_path / "second"
    _v2_plugin(other, "alpha")
    _v2_plugin(other, "b_copy")
    manifest_path = other / "b_copy" / "plugin.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["name"] = "ALPHA"
    copy = PluginManifest.model_validate(data)
    copy.sign_v2(other / "b_copy", SECRET)
    manifest_path.write_text(yaml.safe_dump(copy.model_dump(exclude_none=True)), encoding="utf-8")
    with pytest.raises(PermissionError, match="already registered: ALPHA"):
        _load(other)


# --- bytecode ----------------------------------------------------------------

@pytest.mark.parametrize("planted", ["__pycache__/helper.cpython-311.pyc", "helper.pyc", "sub/__pycache__/x.pyc", "__pycache__/README"])
def test_plugin_with_bytecode_is_refused_at_load(tmp_path, planted):
    plugin_dir = _v2_plugin(tmp_path, "deploy")
    target = plugin_dir / planted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00bytecode")
    with pytest.raises(PermissionError, match="compiled bytecode"):
        _load(tmp_path)


def test_signing_refuses_bytecode(tmp_path):
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("", encoding="utf-8")
    (plugin_dir / "__pycache__").mkdir()
    (plugin_dir / "__pycache__" / "plugin.cpython-311.pyc").write_bytes(b"\x00")
    with pytest.raises(PermissionError, match="compiled bytecode"):
        PluginManifest(name="p", sandbox="docker").sign_v2(plugin_dir, SECRET)


def test_legacy_plugin_with_bytecode_is_refused(tmp_path):
    plugin_dir = _v1_plugin(tmp_path)
    (plugin_dir / "helper.pyc").write_bytes(b"\x00")
    with pytest.raises(PermissionError, match="compiled bytecode"):
        _load(tmp_path, trusted_only=False, allow_legacy_v1_signatures=True)


# --- re-verification before every call --------------------------------------

def test_subprocess_plugin_is_reverified_before_each_call(tmp_path, monkeypatch):
    plugin_dir = _v2_plugin(tmp_path, "deploy")
    _, tools = _load(tmp_path)
    tool = tools.get("deploy")
    assert type(tool) is SubprocessPluginTool
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)

    assert asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(Allow()))).ok
    assert spawns.argv[0][:3] == ("python3", "-I", "-B")

    (plugin_dir / "helper.py").write_text("VALUE = 2  # changed after load\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="signature mismatch"):
        asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(Allow())))
    assert len(spawns.argv) == 1

    (plugin_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugin_dir / "__pycache__").mkdir()
    (plugin_dir / "__pycache__" / "helper.cpython-311.pyc").write_bytes(b"\x00")
    with pytest.raises(PermissionError, match="compiled bytecode"):
        asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(Allow())))
    assert len(spawns.argv) == 1

    # Registry.call reports the refusal instead of running the plugin.
    result = asyncio.run(tools.call("deploy", "call", {"plugin_action": "go"}, _ctx(Allow())))
    assert result.ok is False and "compiled bytecode" in str(result.error)
    assert len(spawns.argv) == 1


def test_docker_plugin_is_reverified_before_each_call(tmp_path, monkeypatch):
    plugin_dir = _v2_plugin(tmp_path, "deploy", sandbox="docker")
    _, tools = _load(tmp_path)
    tool = tools.get("deploy")
    assert type(tool) is DockerPluginTool
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)
    permissions = Allow()

    assert asyncio.run(tool.call("call", {"plugin_action": "go", "plugin_args": {"n": 1}}, _ctx(permissions))).ok
    assert permissions.proposals[0].args["plugin_args"] == {"n": 1}
    assert permissions.proposals[0].args["sandbox"] == "docker"

    (plugin_dir / "plugin.py").write_text("import os\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(permissions)))
    assert len(spawns.argv) == 1


def test_remote_docker_plugin_is_reverified_before_upload(tmp_path, monkeypatch):
    plugin_dir = _v2_plugin(tmp_path, "deploy", sandbox="docker")
    app_config = AppConfig(sandbox=SandboxConfig(backend="remote_docker", runner_url="https://runner.example.test"))
    _, tools = _load(tmp_path, app_config)
    tool = tools.get("deploy")
    assert type(tool) is RemoteDockerPluginTool
    uploads: list[dict[str, Any]] = []

    async def run_command(self, **kwargs: Any) -> RemoteSandboxResult:
        uploads.append(kwargs)
        return RemoteSandboxResult(ok=True, exit_code=0, stdout="{}", stderr="")

    monkeypatch.setattr(RemoteSandboxClient, "run_command", run_command)
    permissions = Allow()
    assert asyncio.run(tool.call("call", {"plugin_action": "go", "plugin_args": {"q": "x"}}, _ctx(permissions))).ok
    assert permissions.proposals[0].args["plugin_args"] == {"q": "x"}

    (plugin_dir / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="signature mismatch"):
        asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(permissions)))
    assert len(uploads) == 1


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform without symlinks
        pytest.skip(f"symlinks unavailable: {exc}")


def test_retargeting_a_symlinked_plugin_dir_cannot_run_tampered_files(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    v1 = _v2_plugin(releases, "v1")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _symlink(plugins / "current", v1)
    # The manifest name is "v1"; the registry only loads what the symlink pointed at.
    _, tools = _load(plugins)
    tool = tools.get("v1")
    spawns = _Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawns)

    # Point the link at a clean signed copy, then tamper with the original.
    clean = releases / "clean"
    clean.mkdir()
    for f in v1.iterdir():
        (clean / f.name).write_bytes(f.read_bytes())
    (plugins / "current").unlink()
    _symlink(plugins / "current", clean)
    (v1 / "helper.py").write_text("print('UNSIGNED CODE RUNS')\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="signature mismatch"):
        asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(Allow())))
    assert spawns.argv == []


def test_each_call_runs_a_verified_copy_not_the_live_directory(tmp_path, monkeypatch):
    plugin_dir = _v2_plugin(tmp_path, "deploy")
    _, tools = _load(tmp_path)
    tool = tools.get("deploy")
    seen: dict[str, Any] = {}

    def on_spawn(argv, kwargs):
        # The original changes after the copy was verified, while the call starts.
        (plugin_dir / "helper.py").write_text("VALUE = 'tampered'\n", encoding="utf-8")
        entry = Path(argv[3])
        seen["entry"] = entry
        seen["helper"] = (entry.parent / "helper.py").read_text(encoding="utf-8")
        seen["cwd"] = kwargs["cwd"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawns(on_spawn))
    assert asyncio.run(tool.call("call", {"plugin_action": "go"}, _ctx(Allow()))).ok
    assert seen["helper"] == "VALUE = 1\n"
    assert plugin_dir.resolve() not in seen["entry"].parents
    assert seen["cwd"] == str(seen["entry"].parent)
    assert not seen["entry"].exists()  # the copy is removed after the call


def test_docker_and_remote_plugins_get_the_verified_copy(tmp_path, monkeypatch):
    plugin_dir = _v2_plugin(tmp_path / "local", "deploy", sandbox="docker")
    _, tools = _load(tmp_path / "local")
    mounts: list[str] = []

    def on_spawn(argv, kwargs):
        mount = argv[argv.index("--mount") + 1]
        src = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        mounts.append(str(src))
        assert (src / "helper.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        # Readable by the container's unprivileged user even under a strict umask.
        assert oct(src.stat().st_mode & 0o777) == oct(0o755)
        assert oct((src / "helper.py").stat().st_mode & 0o777) == oct(0o644)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _Spawns(on_spawn))
    previous = os.umask(0o077)
    try:
        assert asyncio.run(tools.get("deploy").call("call", {"plugin_action": "go"}, _ctx(Allow()))).ok
    finally:
        os.umask(previous)
    assert mounts and Path(mounts[0]).resolve() != plugin_dir.resolve()

    remote_dir = _v2_plugin(tmp_path / "remote", "deploy", sandbox="docker")
    app_config = AppConfig(sandbox=SandboxConfig(backend="remote_docker", runner_url="https://runner.example.test"))
    _, remote_tools = _load(tmp_path / "remote", app_config)
    workspaces: list[Path] = []

    async def run_command(self, **kwargs: Any) -> RemoteSandboxResult:
        workspaces.append(kwargs["workspace"])
        assert (kwargs["workspace"] / kwargs["argv"][2]).is_file()
        return RemoteSandboxResult(ok=True, exit_code=0, stdout="{}", stderr="")

    monkeypatch.setattr(RemoteSandboxClient, "run_command", run_command)
    assert asyncio.run(remote_tools.get("deploy").call("call", {"plugin_action": "go"}, _ctx(Allow()))).ok
    assert workspaces and workspaces[0].resolve() != remote_dir.resolve()


# --- sign_plugin.py migration ------------------------------------------------

def _sign_plugin_main():
    spec = importlib.util.spec_from_file_location("sign_plugin_script_migrate", ROOT / "scripts" / "sign_plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.main


def test_migration_needs_migrate_and_lists_fields_and_files(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path)
    before = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    helper_digest = hashlib.sha256((plugin_dir / "helper.py").read_bytes()).hexdigest()

    assert main(["sign_plugin.py", str(plugin_dir)]) == 1
    err = capsys.readouterr().err
    assert "re-run with --migrate" in err
    assert "sandbox=docker, trusted, permissions=['network.write']" in err
    assert f"file: helper.py  sha256={helper_digest}" in err
    assert "file: plugin.py" in err and "plugin.yaml" not in err.split("fields:")[1]
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8") == before

    # --force alone does not migrate either.
    assert main(["sign_plugin.py", "--force", str(plugin_dir)]) == 1
    assert "--migrate" in capsys.readouterr().err
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8") == before

    assert main(["sign_plugin.py", "--migrate", str(plugin_dir)]) == 0
    out = capsys.readouterr().out
    assert f"file: helper.py  sha256={helper_digest}" in out and "signed with a v2 signature" in out
    PluginManifest.load(plugin_dir / "plugin.yaml").verify(plugin_dir, SECRET)


def test_migration_refuses_bytecode(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path)
    (plugin_dir / "__pycache__").mkdir()
    (plugin_dir / "__pycache__" / "helper.cpython-311.pyc").write_bytes(b"\x00")
    before = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    assert main(["sign_plugin.py", "--migrate", "--force", str(plugin_dir)]) == 1
    assert "compiled bytecode" in capsys.readouterr().err
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8") == before


def test_signing_an_existing_v2_plugin_needs_no_migrate_but_lists_the_files(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v2_plugin(tmp_path, "deploy")
    assert main(["sign_plugin.py", str(plugin_dir)]) == 0
    captured = capsys.readouterr()
    assert "--migrate" not in captured.err
    assert "signing these manifest fields and files" in captured.out and "file: helper.py  sha256=" in captured.out


def test_removing_the_v1_signature_line_does_not_skip_migration(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path)
    manifest_path = plugin_dir / "plugin.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    del data["signature"]
    manifest_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = manifest_path.read_text(encoding="utf-8")

    assert main(["sign_plugin.py", str(plugin_dir)]) == 1
    err = capsys.readouterr().err
    assert "re-run with --migrate" in err and "file: helper.py" in err
    assert manifest_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("flags", [["--check", "--migrate"], ["--check", "--force"], ["--migrate", "--bogus"]])
def test_sign_plugin_rejects_conflicting_flags(tmp_path, flags):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path)
    assert main(["sign_plugin.py", *flags, str(plugin_dir)]) == 64
