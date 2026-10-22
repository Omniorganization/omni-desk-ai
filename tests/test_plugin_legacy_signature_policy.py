from __future__ import annotations

import hashlib
import hmac
import importlib.util
import logging
from pathlib import Path

import pytest
import yaml

from omnidesk_agent.config import AppConfig, PluginConfig
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.plugins.manifest import PluginManifest, find_manifest
from omnidesk_agent.plugins.registry import PluginRegistry
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.validation.production import REQUIRE_PRODUCTION_GUARDS_ENV

SECRET = "secret"
ROOT = Path(__file__).resolve().parents[1]


class Tools:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


@pytest.fixture(autouse=True)
def _non_production_env(monkeypatch):
    for name in ("OMNIDESK_ENV", "APP_ENV", "ENV", "KUBERNETES_SERVICE_HOST", REQUIRE_PRODUCTION_GUARDS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIDESK_PLUGIN_SIGNING_SECRET", SECRET)


def _v1_plugin(root: Path, name: str = "legacy", *, sandbox: str = "subprocess", trusted: bool = True) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    entry = plugin_dir / "plugin.py"
    entry.write_text("def call(action, args):\n    return {'ok': True}\n", encoding="utf-8")
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    # v1 signs the entrypoint digest only; everything below is unsigned.
    manifest = {
        "name": name,
        "trusted": trusted,
        "sandbox": sandbox,
        "permissions": ["network.write", "files.write"],
        "sha256": digest,
        "signature": hmac.new(SECRET.encode(), digest.encode(), hashlib.sha256).hexdigest(),
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (plugin_dir / "helper.py").write_text("UNSIGNED = True\n", encoding="utf-8")
    return plugin_dir


def _v2_plugin(root: Path, name: str = "modern") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("def call(action, args):\n    return {}\n", encoding="utf-8")
    manifest = PluginManifest(name=name, trusted=True, sandbox="docker", entrypoint="plugin.py", permissions=["plugin.call"])
    manifest.sign_v2(plugin_dir, SECRET)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest.model_dump(exclude_none=True)), encoding="utf-8")
    return plugin_dir


@pytest.mark.parametrize("app_config", [None, AppConfig()])
def test_registry_rejects_v1_signature_by_default_outside_production(tmp_path, app_config):
    _v1_plugin(tmp_path)
    tools = Tools()
    with pytest.raises(PermissionError, match="v2 manifest signature"):
        PluginRegistry(tmp_path, PluginConfig(trusted_only=False)).load_into(tools, app_config)
    assert tools.tools == {}


@pytest.mark.parametrize("app_config", [None, AppConfig()])
def test_legacy_opt_in_is_ignored_in_production(tmp_path, monkeypatch, caplog, app_config):
    # Production comes from the environment even when no runtime config is passed.
    monkeypatch.setenv("OMNIDESK_ENV", "production")
    _v1_plugin(tmp_path, sandbox="docker")
    cfg = PluginConfig(trusted_only=False, allow_legacy_v1_signatures=True)
    with caplog.at_level(logging.WARNING, logger="omnidesk_agent.plugins.registry"):
        with pytest.raises(PermissionError, match="v2 manifest signature"):
            PluginRegistry(tmp_path, cfg).load_into(Tools(), app_config)
    assert "ignored in production" in caplog.text


def test_plugin_cannot_shadow_a_registered_tool(tmp_path):
    _v2_plugin(tmp_path, name="vision")
    tools = ToolRegistry()
    builtin = type("Builtin", (), {"name": "vision"})()
    tools.register(builtin)
    with pytest.raises(PermissionError, match="already registered: vision"):
        PluginRegistry(tmp_path, PluginConfig()).load_into(tools, AppConfig())
    assert tools.get("vision") is builtin


def test_legacy_plugin_cannot_take_another_plugins_name(tmp_path):
    _v2_plugin(tmp_path / "plugins", name="deploy")
    # Directory sorts after the signed plugin; its v1 manifest claims the same name.
    _v1_plugin(tmp_path / "plugins", name="z_copy", sandbox="docker", trusted=False)
    manifest_path = tmp_path / "plugins" / "z_copy" / "plugin.yaml"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8").replace("name: z_copy", "name: deploy"), encoding="utf-8")
    cfg = PluginConfig(trusted_only=False, allow_legacy_v1_signatures=True, allowlist=["deploy"])
    tools = ToolRegistry()
    with pytest.raises(PermissionError, match="already registered: deploy"):
        PluginRegistry(tmp_path / "plugins", cfg).load_into(tools, AppConfig())
    assert tools.get("deploy").entrypoint.parent.name == "deploy"


def test_legacy_plugin_cannot_claim_trust(tmp_path):
    _v1_plugin(tmp_path)
    cfg = PluginConfig(trusted_only=True, allow_legacy_v1_signatures=True)
    tools = Tools()
    with pytest.raises(PermissionError, match="cannot establish trust"):
        PluginRegistry(tmp_path, cfg).load_into(tools, AppConfig())
    assert tools.tools == {}


def test_legacy_plugin_runs_untrusted_in_docker_without_permissions(tmp_path, caplog):
    _v1_plugin(tmp_path, sandbox="subprocess", trusted=True)
    registry = PluginRegistry(tmp_path, PluginConfig(trusted_only=False, allow_legacy_v1_signatures=True))
    tools = Tools()
    with caplog.at_level(logging.WARNING, logger="omnidesk_agent.plugins.registry"):
        result = registry.load_into(tools, AppConfig())

    assert result == {"legacy": ["legacy"]}
    tool = tools.tools["legacy"]
    assert type(tool) is DockerPluginTool
    assert tool.permissions == []
    assert "--network" in tool.docker_argv() and "none" in tool.docker_argv()
    loaded = registry.loaded["legacy"]
    assert (loaded.trusted, loaded.sandbox, loaded.permissions) == (False, "docker", [])
    assert "legacy v1 signature" in caplog.text


def test_v2_plugin_keeps_signed_fields(tmp_path):
    _v2_plugin(tmp_path)
    registry = PluginRegistry(tmp_path, PluginConfig())
    tools = Tools()
    registry.load_into(tools, AppConfig())
    assert tools.tools["modern"].permissions == ["plugin.call"]
    assert registry.loaded["modern"].trusted is True


@pytest.mark.parametrize("version", [0, 3, -1])
def test_unknown_signature_version_is_rejected(tmp_path, version):
    plugin_dir = _v2_plugin(tmp_path)
    manifest = PluginManifest.load(plugin_dir / "plugin.yaml").model_copy(update={"signature_version": version})
    with pytest.raises(PermissionError, match="unsupported plugin signature_version"):
        manifest.verify(plugin_dir, SECRET, allow_legacy_v1=True)


def test_more_than_one_manifest_is_refused(tmp_path):
    plugin_dir = _v2_plugin(tmp_path)
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="more than one manifest"):
        find_manifest(plugin_dir)
    with pytest.raises(PermissionError, match="more than one manifest"):
        PluginRegistry(tmp_path, PluginConfig()).load_into(Tools(), AppConfig())


def _sign_plugin_main():
    spec = importlib.util.spec_from_file_location("sign_plugin_script", ROOT / "scripts" / "sign_plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.main


def test_sign_plugin_script_migrates_v1_to_v2(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path, sandbox="docker")

    assert main(["sign_plugin.py", "--check", str(plugin_dir)]) == 1
    assert "legacy v1 signature" in capsys.readouterr().err

    assert main(["sign_plugin.py", str(plugin_dir)]) == 0
    out = capsys.readouterr().out
    assert "signature v2" in out and "sandbox=docker" in out

    signed = PluginManifest.load(plugin_dir / "plugin.yaml")
    assert signed.signature_version == 2
    signed.verify(plugin_dir, SECRET)
    assert main(["sign_plugin.py", "--check", str(plugin_dir)]) == 0

    (plugin_dir / "helper.py").write_text("UNSIGNED = False\n", encoding="utf-8")
    assert main(["sign_plugin.py", "--check", str(plugin_dir)]) == 1
    assert "signature mismatch" in capsys.readouterr().err


def test_sign_plugin_script_signs_the_manifest_the_registry_loads(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path, sandbox="docker")
    (plugin_dir / "plugin.json").write_text("{}", encoding="utf-8")
    assert main(["sign_plugin.py", str(plugin_dir)]) == 65
    assert "more than one manifest" in capsys.readouterr().err
    assert PluginManifest.load(plugin_dir / "plugin.yaml").signature_version == 1


def test_sign_plugin_script_refuses_to_bless_a_modified_plugin(tmp_path, capsys):
    main = _sign_plugin_main()
    plugin_dir = _v1_plugin(tmp_path, sandbox="docker")
    # Entrypoint changed after the v1 signature was made.
    (plugin_dir / "plugin.py").write_text("import os\n", encoding="utf-8")
    before = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")

    assert main(["sign_plugin.py", str(plugin_dir)]) == 1
    assert "existing signature does not verify" in capsys.readouterr().err
    assert (plugin_dir / "plugin.yaml").read_text(encoding="utf-8") == before

    assert main(["sign_plugin.py", "--force", str(plugin_dir)]) == 0
    PluginManifest.load(plugin_dir / "plugin.yaml").verify(plugin_dir, SECRET)

    # Same for a v2 plugin whose files changed since signing.
    (plugin_dir / "helper.py").write_text("CHANGED = True\n", encoding="utf-8")
    assert main(["sign_plugin.py", str(plugin_dir)]) == 1
    assert main(["sign_plugin.py", "--force", str(plugin_dir)]) == 0


def test_sign_plugin_script_signs_an_unsigned_manifest(tmp_path):
    main = _sign_plugin_main()
    plugin_dir = tmp_path / "fresh"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("def call(action, args):\n    return {}\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text('{"name": "fresh", "sandbox": "docker"}', encoding="utf-8")
    assert main(["sign_plugin.py", str(plugin_dir)]) == 0
    PluginManifest.load(plugin_dir / "plugin.json").verify(plugin_dir, SECRET)


def test_sign_plugin_script_usage_errors(tmp_path, monkeypatch):
    main = _sign_plugin_main()
    assert main(["sign_plugin.py"]) == 64
    assert main(["sign_plugin.py", "--bogus", str(tmp_path)]) == 64
    assert main(["sign_plugin.py", str(tmp_path)]) == 66
    monkeypatch.delenv("OMNIDESK_PLUGIN_SIGNING_SECRET")
    assert main(["sign_plugin.py", str(tmp_path)]) == 64
