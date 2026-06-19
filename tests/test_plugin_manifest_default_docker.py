from __future__ import annotations

from omnidesk_agent.plugins.manifest import PluginManifest


def test_plugin_manifest_defaults_to_docker_sandbox():
    manifest = PluginManifest(name="p", trusted=True, entrypoint="plugin.py", sha256="x", signature="y")
    assert manifest.sandbox == "docker"
