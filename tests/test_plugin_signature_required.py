from __future__ import annotations

import hashlib
import hmac

import pytest

from omnidesk_agent.plugins.manifest import PluginManifest


def test_plugin_requires_sha256_and_signature(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    entry = plugin_dir / "plugin.py"
    entry.write_text("print('ok')\n", encoding="utf-8")

    manifest = PluginManifest(name="p", trusted=True, sandbox="subprocess", entrypoint="plugin.py")
    with pytest.raises(PermissionError, match="sha256 is required"):
        manifest.verify(plugin_dir, "secret")

    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    manifest.sha256 = digest
    with pytest.raises(PermissionError, match="signature is required"):
        manifest.verify(plugin_dir, "secret")

    manifest.sign_v2(plugin_dir, "secret")
    manifest.verify(plugin_dir, "secret")


def _signed_v2_plugin(tmp_path):
    plugin_dir = tmp_path / "p2"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text("import helper\n", encoding="utf-8")
    (plugin_dir / "helper.py").write_text("x = 1\n", encoding="utf-8")
    manifest = PluginManifest(name="p2", trusted=True, sandbox="docker", entrypoint="plugin.py", permissions=["net:none"])
    manifest.sign_v2(plugin_dir, "secret")
    return plugin_dir, manifest


def test_v2_signature_binds_manifest_fields_and_all_files(tmp_path):
    plugin_dir, manifest = _signed_v2_plugin(tmp_path)
    manifest.verify(plugin_dir, "secret")

    downgraded = manifest.model_copy(update={"sandbox": "subprocess"})
    with pytest.raises(PermissionError, match="signature mismatch"):
        downgraded.verify(plugin_dir, "secret")

    escalated = manifest.model_copy(update={"permissions": ["net:any"]})
    with pytest.raises(PermissionError, match="signature mismatch"):
        escalated.verify(plugin_dir, "secret")

    (plugin_dir / "helper.py").write_text("import os; os.system('id')\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="signature mismatch"):
        manifest.verify(plugin_dir, "secret")


def test_v1_manifest_signature_requires_explicit_legacy_opt_in(tmp_path):
    plugin_dir = tmp_path / "p"
    plugin_dir.mkdir()
    entry = plugin_dir / "plugin.py"
    entry.write_text("print('ok')\n", encoding="utf-8")
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    legacy = PluginManifest(name="p", sha256=digest, signature=hmac.new(b"secret", digest.encode("utf-8"), hashlib.sha256).hexdigest())
    with pytest.raises(PermissionError, match="v2 manifest signature"):
        legacy.verify(plugin_dir, "secret")
    legacy.verify(plugin_dir, "secret", allow_legacy_v1=True)
