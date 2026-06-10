from __future__ import annotations
import hashlib
from omnidesk_agent.plugins.manifest import PluginManifest


def test_plugin_manifest_hash_verification(tmp_path):
    plugin = tmp_path / "plugin.py"
    plugin.write_text("print('ok')", encoding="utf-8")
    digest = hashlib.sha256(plugin.read_bytes()).hexdigest()
    manifest = PluginManifest(name="p", trusted=True, sha256=digest)
    manifest.verify(tmp_path)
