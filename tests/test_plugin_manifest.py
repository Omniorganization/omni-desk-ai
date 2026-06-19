from __future__ import annotations

import hashlib
import hmac

import pytest

from omnidesk_agent.plugins.manifest import PluginManifest


def test_plugin_manifest_hash_and_signature_verification(tmp_path):
    plugin = tmp_path / "plugin.py"
    plugin.write_text("print('ok')", encoding="utf-8")
    digest = hashlib.sha256(plugin.read_bytes()).hexdigest()

    manifest = PluginManifest(name="p", trusted=True, sha256=digest)
    with pytest.raises(PermissionError, match="signature is required"):
        manifest.verify(tmp_path)

    manifest.signature = hmac.new(b"secret", digest.encode("utf-8"), hashlib.sha256).hexdigest()
    manifest.verify(tmp_path, "secret")


def test_plugin_manifest_rejects_bad_hash(tmp_path):
    plugin = tmp_path / "plugin.py"
    plugin.write_text("print('ok')", encoding="utf-8")
    manifest = PluginManifest(name="p", trusted=True, sha256="bad", signature="bad")
    with pytest.raises(PermissionError, match="hash mismatch"):
        manifest.verify(tmp_path, "secret")
