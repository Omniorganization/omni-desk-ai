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
_BYTECODE_SUFFIXES = (".pyc", ".pyo")


def reject_bytecode(plugin_dir: Path, name: str) -> None:
    """Refuse compiled bytecode in a plugin directory.

    Python imports a sibling module from a matching __pycache__ entry or a
    bare .pyc without reading the signed source, so bytecode could change
    what a signed plugin runs.
    """
    for path in plugin_dir.rglob("*"):
        rel = path.relative_to(plugin_dir)
        if "__pycache__" in rel.parts or path.suffix.lower() in _BYTECODE_SUFFIXES:
            raise PermissionError(
                f"plugin may not contain compiled bytecode (delete __pycache__ and .pyc files): {name}: {rel.as_posix()}"
            )


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

    def plugin_files(self, plugin_dir: Path) -> dict[str, str]:
        """sha256 of every file a v2 signature binds, keyed by relative path."""
        reject_bytecode(plugin_dir, self.name)
        files: dict[str, str] = {}
        root = plugin_dir.resolve()
        for path in sorted(plugin_dir.rglob("*")):
            rel = path.relative_to(plugin_dir).as_posix()
            if rel in MANIFEST_FILE_NAMES:
                continue
            if path.is_symlink():
                raise PermissionError(f"plugin may not contain symlinks: {self.name}: {rel}")
            if path.is_file():
                if root not in path.resolve().parents:
                    raise PermissionError(f"plugin file escapes plugin directory: {self.name}: {rel}")
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def signing_payload_v2(self, plugin_dir: Path) -> bytes:
        files = self.plugin_files(plugin_dir)
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

    def copy_verified(self, plugin_dir: Path, dest: Path, signing_secret: Optional[str], *, allow_legacy_v1: bool = False) -> None:
        """Copy the plugin directory to ``dest`` (which must not exist) and verify the copy.

        Run the plugin from the copy: what runs is exactly what was verified,
        whatever happens to the original directory afterwards.
        """
        reject_bytecode(plugin_dir, self.name)
        dest.mkdir()
        dest.chmod(0o755)
        for path in sorted(plugin_dir.rglob("*")):
            rel = path.relative_to(plugin_dir)
            target = dest / rel
            if path.is_symlink():
                raise PermissionError(f"plugin may not contain symlinks: {self.name}: {rel.as_posix()}")
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                # Readable by the sandbox user, which is not the gateway user.
                target.chmod(0o644)
            else:
                raise PermissionError(f"plugin may not contain special files: {self.name}: {rel.as_posix()}")
        self.verify(dest, signing_secret, allow_legacy_v1=allow_legacy_v1)

    def verify(self, plugin_dir: Path, signing_secret: Optional[str] = None, *, allow_legacy_v1: bool = False) -> None:
        """Check the entrypoint digest and the HMAC signature.

        A v2 signature is required. ``allow_legacy_v1`` accepts a v1 signature,
        which covers only the entrypoint digest; the caller must then treat
        every other manifest field and every sibling file as unsigned.
        """
        entry = (plugin_dir / self.entrypoint).resolve()
        if not entry.exists():
            raise FileNotFoundError(entry)
        reject_bytecode(plugin_dir, self.name)
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
                raise PermissionError(f"plugin requires a v2 manifest signature (re-sign with scripts/sign_plugin.py --migrate): {self.name}")
            message = digest.encode("utf-8")
        else:
            raise PermissionError(f"unsupported plugin signature_version {self.signature_version}: {self.name}")
        expected = hmac.new(signing_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise PermissionError(f"plugin signature mismatch: {self.name}")
