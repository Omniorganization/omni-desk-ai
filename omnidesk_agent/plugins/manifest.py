from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None
from pydantic import BaseModel, Field


class PluginManifest(BaseModel):
    name: str
    version: str = "0.0.0"
    enabled: bool = True
    trusted: bool = False
    description: str = ""
    entrypoint: str = "plugin.py"
    sandbox: str = "docker"
    permissions: list[str] = Field(default_factory=list)
    signature: Optional[str] = None
    sha256: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        if path.suffix in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is required to load YAML plugin manifests")
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def verify(self, plugin_dir: Path, signing_secret: Optional[str] = None) -> None:
        entry = (plugin_dir / self.entrypoint).resolve()
        if not entry.exists():
            raise FileNotFoundError(entry)
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        if not self.sha256:
            raise PermissionError(f"plugin sha256 is required: {self.name}")
        if not hmac.compare_digest(self.sha256, digest):
            raise PermissionError(f"plugin hash mismatch: {self.name}")
        if not self.signature:
            raise PermissionError(f"plugin signature is required: {self.name}")
        if not signing_secret:
            raise PermissionError(f"plugin signature requires signing secret: {self.name}")
        expected = hmac.new(signing_secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise PermissionError(f"plugin signature mismatch: {self.name}")
