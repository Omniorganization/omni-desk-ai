from __future__ import annotations

import importlib.util
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from omnidesk_agent.config import AppConfig, CapabilitiesConfig
from omnidesk_agent.plugins.manifest import LEGACY_SIGNATURE_VERSION, PluginManifest, find_manifest
from omnidesk_agent.plugins.docker_runner import DockerPluginTool, RemoteDockerPluginTool
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool
from omnidesk_agent.validation.production import is_production_mode

LOGGER = logging.getLogger(__name__)

# Built-in tool names (one capability each), reserved whether or not the
# capability is enabled: a plugin must never look like, or later collide
# with, a built-in tool in plans, approvals or tool policy.
RESERVED_TOOL_NAMES = frozenset(CapabilitiesConfig.model_fields)


def _name_key(name: str) -> str:
    # Names that differ only in case or in -/./_ read the same to an approver.
    return re.sub(r"[-.]", "_", name).lower()


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
        self.default_sandbox = str(getattr(config, "default_sandbox", "docker")) if config is not None and not isinstance(config, bool) else "docker"
        self.production_forbid_subprocess = bool(getattr(config, "production_forbid_subprocess", True)) if config is not None and not isinstance(config, bool) else True
        self.allow_legacy_v1_signatures = bool(getattr(config, "allow_legacy_v1_signatures", False)) if config is not None and not isinstance(config, bool) else False
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
                self._check_name_unused(manifest.name, tool_registry)
                # Without a runtime config the environment still decides.
                production = is_production_mode(app_config if app_config is not None else AppConfig())
                legacy = manifest.signature_version == LEGACY_SIGNATURE_VERSION
                if legacy and production and self.allow_legacy_v1_signatures:
                    LOGGER.warning("plugins.allow_legacy_v1_signatures is ignored in production: %s", manifest.name)
                allow_legacy_v1 = self.allow_legacy_v1_signatures and not production
                # Resolve once: verification and every later copy read this
                # directory, so retargeting a symlink on the way to it later
                # cannot point the checks somewhere else.
                real_dir = plugin_dir.resolve()
                manifest.verify(real_dir, self.signing_secret, allow_legacy_v1=allow_legacy_v1)
                signed = manifest
                if legacy:
                    manifest = self._restrict_legacy(manifest)
                if production and self.production_forbid_subprocess and manifest.sandbox != "docker":
                    raise PermissionError(f"production plugins must use docker sandbox: {manifest.name}")

                entrypoint = (real_dir / manifest.entrypoint).resolve()
                if not entrypoint.is_file() or real_dir not in entrypoint.parents:
                    raise PermissionError(f"plugin entrypoint escapes plugin directory: {manifest.name}")
                prepare_files = self._verified_copier(signed, real_dir, entrypoint.relative_to(real_dir), allow_legacy_v1)
                if manifest.sandbox == "subprocess":
                    tool_registry.register(SubprocessPluginTool(manifest.name, entrypoint, manifest.permissions, timeout_seconds=self.plugin_timeout_seconds, prepare_files=prepare_files))
                    self.loaded[manifest.name] = manifest
                    results[manifest.name] = [manifest.name]
                elif manifest.sandbox == "docker":
                    sandbox_cfg = getattr(app_config, "sandbox", None)
                    if sandbox_cfg is not None and getattr(sandbox_cfg, "backend", "docker") == "remote_docker":
                        if not getattr(sandbox_cfg, "runner_url", None):
                            raise PermissionError(f"remote_docker plugin sandbox requires sandbox.runner_url: {manifest.name}")
                        tool_registry.register(RemoteDockerPluginTool(
                            manifest.name,
                            entrypoint,
                            manifest.permissions,
                            runner_url=str(sandbox_cfg.runner_url),
                            runner_token_env=str(getattr(sandbox_cfg, "runner_token_env", "OMNIDESK_SANDBOX_RUNNER_TOKEN")),
                            runner_hmac_secret_env=str(getattr(sandbox_cfg, "runner_hmac_secret_env", "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET")),
                            timeout_seconds=self.plugin_timeout_seconds,
                            image=str(getattr(sandbox_cfg, "docker_image", "python:3.11-slim")),
                            prepare_files=prepare_files,
                        ))
                    else:
                        tool_registry.register(DockerPluginTool(manifest.name, entrypoint, manifest.permissions, timeout_seconds=self.plugin_timeout_seconds, prepare_files=prepare_files))
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

    def _restrict_legacy(self, manifest: PluginManifest) -> PluginManifest:
        # A v1 signature covers only the entrypoint digest, so trusted, sandbox
        # and permissions in the manifest are unsigned: never trust them.
        if self.trusted_only:
            raise PermissionError(
                f"legacy v1 plugin signature cannot establish trust (re-sign with scripts/sign_plugin.py --migrate): {manifest.name}"
            )
        LOGGER.warning(
            "loading plugin %s with a legacy v1 signature: manifest fields are unsigned, so it runs untrusted "
            "in the docker sandbox with no permissions; re-sign it with scripts/sign_plugin.py",
            manifest.name,
        )
        return manifest.model_copy(update={"trusted": False, "sandbox": "docker", "permissions": []})

    def _verified_copier(self, manifest: PluginManifest, real_dir: Path, entry_rel: Path, allow_legacy_v1: bool) -> Callable[[Path], Path]:
        # The signature was checked against the files on disk at load. Each
        # call copies the directory, verifies the copy and runs the copy, so
        # files changed (or bytecode added) after loading never run.
        secret = self.signing_secret

        def prepare_files(workdir: Path) -> Path:
            copy = workdir / "plugin"
            manifest.copy_verified(real_dir, copy, secret, allow_legacy_v1=allow_legacy_v1)
            return copy / entry_rel

        return prepare_files

    def _check_name_unused(self, name: str, tool_registry) -> None:
        # ToolRegistry.register replaces a tool of the same name, so a plugin
        # must never shadow a built-in tool or another plugin, and must not
        # take a built-in name that is merely disabled right now.
        key = _name_key(name)
        if key in RESERVED_TOOL_NAMES:
            raise PermissionError(f"plugin name is reserved for a built-in tool: {name}")
        names = getattr(tool_registry, "names", None)
        registered = set(names()) if callable(names) else set(getattr(tool_registry, "tools", {}) or {})
        if key in {_name_key(other) for other in (*self.loaded, *registered)}:
            raise PermissionError(f"plugin name is already registered: {name}")

    @staticmethod
    def _check_manifest_name(name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name or ""):
            raise PermissionError(f"invalid plugin name: {name!r}")

    @staticmethod
    def _manifest_path(plugin_dir: Path) -> Optional[Path]:
        return find_manifest(plugin_dir)
