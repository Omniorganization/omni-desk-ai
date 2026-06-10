from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from omnidesk_agent.plugins.manifest import PluginManifest
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool


class PluginRegistry:
    def __init__(self, plugins_dir: Path, *, trusted_only: bool = True, allowlist: list[str] | None = None, signing_secret_env: str | None = "OMNIDESK_PLUGIN_SIGNING_SECRET"):
        self.plugins_dir = plugins_dir.expanduser()
        self.trusted_only = trusted_only
        self.allowlist = set(allowlist or [])
        self.signing_secret = os.getenv(signing_secret_env or "") if signing_secret_env else None
        self.loaded: dict[str, PluginManifest] = {}

    def load_into(self, tool_registry, app_config: Any | None = None) -> dict[str, list[str]]:
        results: dict[str, list[str]] = {}
        if not self.plugins_dir.exists():
            return results
        for plugin_dir in sorted(p for p in self.plugins_dir.iterdir() if p.is_dir()):
            manifest_path = self._manifest_path(plugin_dir)
            if not manifest_path:
                continue
            manifest = PluginManifest.load(manifest_path)
            if not manifest.enabled:
                continue
            if self.allowlist and manifest.name not in self.allowlist:
                continue
            if self.trusted_only and not manifest.trusted:
                continue
            manifest.verify(plugin_dir, self.signing_secret)

            entrypoint = (plugin_dir / manifest.entrypoint).resolve()
            if manifest.sandbox == "subprocess":
                tool_registry.register(SubprocessPluginTool(manifest.name, entrypoint, manifest.permissions))
                self.loaded[manifest.name] = manifest
                results[manifest.name] = [manifest.name]
            elif manifest.sandbox == "in_process":
                # Explicitly allowed only for trusted plugins that passed hash/signature checks.
                names = self._load_in_process(manifest, entrypoint, tool_registry, app_config)
                self.loaded[manifest.name] = manifest
                results[manifest.name] = names
            else:
                raise ValueError(f"Unsupported plugin sandbox: {manifest.sandbox}")
        return results

    def _load_in_process(self, manifest: PluginManifest, entrypoint: Path, tool_registry, app_config: Any | None) -> list[str]:
        spec = importlib.util.spec_from_file_location(f"omnidesk_plugin_{manifest.name}", str(entrypoint))
        if not spec or not spec.loader:
            raise RuntimeError(f"Cannot load plugin: {manifest.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "register"):
            raise RuntimeError(f"Plugin has no register(): {manifest.name}")
        result = module.register(tool_registry, app_config=app_config)
        return list(result or [])

    @staticmethod
    def _manifest_path(plugin_dir: Path) -> Path | None:
        for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
            p = plugin_dir / name
            if p.exists():
                return p
        return None
