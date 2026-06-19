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

    manifest.signature = hmac.new(b"secret", digest.encode("utf-8"), hashlib.sha256).hexdigest()
    manifest.verify(plugin_dir, "secret")
