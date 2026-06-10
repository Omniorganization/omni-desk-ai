from __future__ import annotations
import importlib.util, json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml
from omnidesk_agent.config import PluginConfig
from omnidesk_agent.tools.registry import ToolRegistry

@dataclass(slots=True)
class Plugin:
    name: str
    path: Path
    trusted: bool = False
    enabled: bool = True
    tools: list[str] = field(default_factory=list)

class PluginRegistry:
    def __init__(self, plugin_dirs: list[Path], cfg: PluginConfig):
        self.plugin_dirs = plugin_dirs
        self.cfg = cfg
        self.plugins: dict[str, Plugin] = {}
    def discover(self) -> dict[str, Plugin]:
        self.plugins.clear()
        for root in self.plugin_dirs:
            root = root.expanduser()
            if not root.exists():
                continue
            manifests = list(root.glob("*/plugin.yaml")) + list(root.glob("*/plugin.yml")) + list(root.glob("*/plugin.json"))
            for manifest in manifests:
                meta = self._read_manifest(manifest)
                name = str(meta.get("name") or manifest.parent.name)
                self.plugins[name] = Plugin(name=name, path=manifest.parent, trusted=bool(meta.get("trusted", False)), enabled=bool(meta.get("enabled", True)))
        return self.plugins
    def load_into(self, tools: ToolRegistry, app_config: Any | None = None) -> dict[str, Plugin]:
        if not self.cfg.enabled:
            return {}
        self.discover()
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            if self.cfg.allowlist and plugin.name not in self.cfg.allowlist:
                continue
            if self.cfg.trusted_only and not plugin.trusted:
                continue
            module_path = plugin.path / "plugin.py"
            if not module_path.exists():
                continue
            spec = importlib.util.spec_from_file_location(f"omnidesk_plugin_{plugin.name}", module_path)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if callable(register):
                result = register(tools, app_config)
                if isinstance(result, list):
                    plugin.tools = [str(x) for x in result]
        return self.plugins
    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if path.suffix == ".json" else (yaml.safe_load(text) or {})
