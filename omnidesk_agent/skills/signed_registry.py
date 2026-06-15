from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class SkillPermissionManifest:
    name: str
    version: str
    sandbox_profile: Literal[
        "profile_readonly",
        "profile_workspace_write",
        "profile_workspace_write_no_network",
        "profile_tool_limited",
        "profile_break_glass",
    ] = "profile_readonly"
    permissions: tuple[str, ...] = ()
    sha256: str = ""
    signature: str = ""
    update_approval: Literal["none", "operator", "owner"] = "owner"
    vulnerability_scan: Literal["not_run", "passed", "blocked"] = "not_run"
    denied_permissions: tuple[str, ...] = field(default_factory=lambda: ("read_secret", "change_policy", "self_modify", "bypass_approval"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SignedSkillRegistry:
    """Verify SKILL.md packages before planner exposure."""

    def __init__(self, skill_dirs: list[Path], *, signing_secret: str | None = None):
        self.skill_dirs = [path.expanduser() for path in skill_dirs]
        self.signing_secret = signing_secret

    def verify_skill(self, skill_dir: Path) -> SkillPermissionManifest:
        manifest_path = skill_dir / "skill.manifest.json"
        skill_path = skill_dir / "SKILL.md"
        if not manifest_path.exists():
            raise PermissionError(f"skill permission manifest is required: {skill_dir}")
        if not skill_path.exists():
            raise FileNotFoundError(skill_path)
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = SkillPermissionManifest(
            name=data["name"],
            version=data.get("version", "0.0.0"),
            sandbox_profile=data.get("sandbox_profile", "profile_readonly"),
            permissions=tuple(data.get("permissions", ())),
            sha256=data.get("sha256", ""),
            signature=data.get("signature", ""),
            update_approval=data.get("update_approval", "owner"),
            vulnerability_scan=data.get("vulnerability_scan", "not_run"),
            denied_permissions=tuple(data.get("denied_permissions", ("read_secret", "change_policy", "self_modify", "bypass_approval"))),
        )
        digest = hashlib.sha256(skill_path.read_bytes()).hexdigest()
        if not manifest.sha256:
            raise PermissionError(f"skill sha256 is required: {manifest.name}")
        if not hmac.compare_digest(manifest.sha256, digest):
            raise PermissionError(f"skill hash mismatch: {manifest.name}")
        if set(manifest.permissions).intersection(manifest.denied_permissions):
            raise PermissionError(f"skill requests denied permissions: {manifest.name}")
        if not manifest.signature:
            raise PermissionError(f"skill signature is required: {manifest.name}")
        if not self.signing_secret:
            raise PermissionError(f"skill signature requires signing secret: {manifest.name}")
        expected = hmac.new(self.signing_secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, manifest.signature):
            raise PermissionError(f"skill signature mismatch: {manifest.name}")
        if manifest.vulnerability_scan != "passed":
            raise PermissionError(f"skill vulnerability scan must pass: {manifest.name}")
        return manifest

    def verified_index(self) -> list[dict[str, Any]]:
        verified: list[dict[str, Any]] = []
        for root in self.skill_dirs:
            if not root.exists():
                continue
            for skill in sorted(path for path in root.iterdir() if path.is_dir()):
                verified.append(self.verify_skill(skill).to_dict())
        return verified
