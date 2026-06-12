from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.plugins.manifest import PluginManifest
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool


class PluginRegistry:
    def __init__(
        self,
        plugins_dir,
        config=None,
        *,
        trusted_only: Optional[bool] = None,
        allowlist: Optional[list[str]] = None,
        signing_secret_env: Optional[str] = "OMNIDESK_PLUGIN_SIGNING_SECRET",
    ):
        dirs = plugins_dir if isinstance(plugins_dir, (list, tuple)) else [plugins_dir]
        self.plugins_dirs = [Path(d).expanduser() for d in dirs]
        if config is not None and not isinstance(config, bool):
            self.trusted_only = bool(getattr(config, "trusted_only", True))
            self.allowlist = set(getattr(config, "allowlist", []) or [])
        else:
            self.trusted_only = bool(config) if isinstance(config, bool) else (True if trusted_only is None else trusted_only)
            self.allowlist = set(allowlist or [])
        self.signing_secret = os.getenv(signing_secret_env or "") if signing_secret_env else None
        self.allow_in_process = bool(getattr(config, "allow_in_process", False)) if config is not None and not isinstance(config, bool) else False
        self.plugin_timeout_seconds = int(getattr(config, "plugin_timeout_seconds", 30)) if config is not None and not isinstance(config, bool) else 30
        self.loaded: dict[str, PluginManifest] = {}

    @property
    def plugins(self) -> dict[str, PluginManifest]:
        return dict(self.loaded)

    def load_into(self, tool_registry, app_config: Optional[Any] = None) -> dict[str, list[str]]:
        results: dict[str, list[str]] = {}
        for root in self.plugins_dirs:
            if not root.exists():
                continue
            for plugin_dir in sorted(p for p in root.iterdir() if p.is_dir()):
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
                self._check_manifest_name(manifest.name)
                manifest.verify(plugin_dir, self.signing_secret)

                entrypoint = (plugin_dir / manifest.entrypoint).resolve()
                if not entrypoint.is_file() or plugin_dir.resolve() not in entrypoint.parents:
                    raise PermissionError(f"plugin entrypoint escapes plugin directory: {manifest.name}")
                if manifest.sandbox == "subprocess":
                    tool_registry.register(SubprocessPluginTool(manifest.name, entrypoint, manifest.permissions, timeout_seconds=self.plugin_timeout_seconds))
                    self.loaded[manifest.name] = manifest
                    results[manifest.name] = [manifest.name]
                elif manifest.sandbox == "docker":
                    tool_registry.register(DockerPluginTool(manifest.name, entrypoint, manifest.permissions, timeout_seconds=self.plugin_timeout_seconds))
                    self.loaded[manifest.name] = manifest
                    results[manifest.name] = [manifest.name]
                elif manifest.sandbox == "in_process":
                    raise PermissionError(f"in_process plugin sandbox is forbidden in production: {manifest.name}")
                else:
                    raise ValueError(f"Unsupported plugin sandbox: {manifest.sandbox}")
        return results

    def _load_in_process(self, manifest: PluginManifest, entrypoint: Path, tool_registry, app_config: Optional[Any]) -> list[str]:
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
    def _check_manifest_name(name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name or ""):
            raise PermissionError(f"invalid plugin name: {name!r}")

    @staticmethod
    def _manifest_path(plugin_dir: Path) -> Optional[Path]:
        for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
            p = plugin_dir / name
            if p.exists():
                return p
        return None
