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


# Lookup order shared by PluginRegistry and scripts/sign_plugin.py.
MANIFEST_FILE_ORDER: tuple[str, ...] = ("plugin.yaml", "plugin.yml", "plugin.json")
MANIFEST_FILE_NAMES = frozenset(MANIFEST_FILE_ORDER)
LEGACY_SIGNATURE_VERSION = 1
CURRENT_SIGNATURE_VERSION = 2


def find_manifest(plugin_dir: Path) -> Optional[Path]:
    """Return the plugin manifest path, refusing directories with more than one."""
    found = [plugin_dir / name for name in MANIFEST_FILE_ORDER if (plugin_dir / name).exists()]
    if len(found) > 1:
        raise PermissionError(f"plugin has more than one manifest: {plugin_dir.name}: {[p.name for p in found]}")
    return found[0] if found else None


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
    # v1 signs only the entrypoint digest. v2 also binds the security-relevant
    # manifest fields (sandbox, permissions, trusted, ...) and every file in the
    # plugin directory, so editing plugin.yaml or a sibling module is detected.
    # A manifest without this field is a legacy v1 manifest.
    signature_version: int = LEGACY_SIGNATURE_VERSION

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        if path.suffix in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is required to load YAML plugin manifests")
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def signing_payload_v2(self, plugin_dir: Path) -> bytes:
        files: dict[str, str] = {}
        root = plugin_dir.resolve()
        for path in sorted(plugin_dir.rglob("*")):
            rel = path.relative_to(plugin_dir).as_posix()
            if "__pycache__" in path.parts or rel in MANIFEST_FILE_NAMES:
                continue
            if path.is_symlink():
                raise PermissionError(f"plugin may not contain symlinks: {self.name}: {rel}")
            if path.is_file():
                if root not in path.resolve().parents:
                    raise PermissionError(f"plugin file escapes plugin directory: {self.name}: {rel}")
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = {
            "v": 2,
            "name": self.name,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "sandbox": self.sandbox,
            "permissions": sorted(self.permissions),
            "trusted": self.trusted,
            "files": files,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sign_v2(self, plugin_dir: Path, signing_secret: str) -> None:
        """Set sha256 and a v2 signature for this manifest (operator tooling)."""
        entry = (plugin_dir / self.entrypoint).resolve()
        self.sha256 = hashlib.sha256(entry.read_bytes()).hexdigest()
        self.signature_version = CURRENT_SIGNATURE_VERSION
        self.signature = hmac.new(signing_secret.encode("utf-8"), self.signing_payload_v2(plugin_dir), hashlib.sha256).hexdigest()

    def verify(self, plugin_dir: Path, signing_secret: Optional[str] = None, *, allow_legacy_v1: bool = False) -> None:
        """Check the entrypoint digest and the HMAC signature.

        A v2 signature is required. ``allow_legacy_v1`` accepts a v1 signature,
        which covers only the entrypoint digest; the caller must then treat
        every other manifest field and every sibling file as unsigned.
        """
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
        if self.signature_version == CURRENT_SIGNATURE_VERSION:
            message = self.signing_payload_v2(plugin_dir)
        elif self.signature_version == LEGACY_SIGNATURE_VERSION:
            if not allow_legacy_v1:
                raise PermissionError(f"plugin requires a v2 manifest signature (re-sign with scripts/sign_plugin.py): {self.name}")
            message = digest.encode("utf-8")
        else:
            raise PermissionError(f"unsupported plugin signature_version {self.signature_version}: {self.name}")
        expected = hmac.new(signing_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise PermissionError(f"plugin signature mismatch: {self.name}")
