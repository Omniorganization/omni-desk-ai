from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from omnidesk_agent.config import UpdateConfig


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_tuple(value: str) -> tuple[int, int, int]:
    core = str(value or "0.0.0").split("+", 1)[0].split("-", 1)[0]
    parts = []
    for item in core.split(".")[:3]:
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


@dataclass(frozen=True)
class ManifestBundle:
    manifest: dict[str, Any]
    sha256: str
    source: str
    base_dir: Path | None = None


@dataclass(frozen=True)
class SignatureVerification:
    valid: bool
    valid_count: int
    algorithms: list[str]
    reason: str = ""


@dataclass(frozen=True)
class UpdatePolicyDecision:
    can_download: bool
    can_stage: bool
    can_activate: bool
    reason: str
    channel: str


class UpdateAuditLog:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **payload: Any) -> None:
        item = {"event": event, **payload}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


class UpdateManifestClient:
    def __init__(self, manifest_url: str):
        self.manifest_url = manifest_url

    def fetch_latest(self) -> ManifestBundle:
        source = self.manifest_url
        if not source:
            raise RuntimeError("updates.manifest_url is required")
        if source.startswith("http://") or source.startswith("https://"):
            with urllib.request.urlopen(source, timeout=15) as response:  # nosec B310 - operator-configured update manifest URL
                raw = response.read()
            base_dir = None
        else:
            path = Path(source[7:] if source.startswith("file://") else source).expanduser()
            raw = path.read_bytes()
            base_dir = path.parent
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("release manifest must be a JSON object")
        return ManifestBundle(manifest=manifest, sha256=hashlib.sha256(raw).hexdigest(), source=source, base_dir=base_dir)


class SignatureVerifier:
    def __init__(self, *, public_key: str | None = None, public_key_file: Path | None = None):
        self.public_key = public_key
        self.public_key_file = Path(public_key_file).expanduser() if public_key_file else None

    @staticmethod
    def canonical_payload(manifest: dict[str, Any]) -> bytes:
        signable = {k: v for k, v in manifest.items() if k not in {"signature", "signatures"}}
        return json.dumps(signable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def verify_manifest(self, manifest: dict[str, Any]) -> SignatureVerification:
        signatures = manifest.get("signatures")
        if signatures is None and manifest.get("signature"):
            signatures = [manifest.get("signature")]
        if not isinstance(signatures, list) or not signatures:
            return SignatureVerification(False, 0, [], "manifest signature is missing")
        payload = self.canonical_payload(manifest)
        valid_count = 0
        algorithms: list[str] = []
        last_error = ""
        for entry in signatures:
            if not isinstance(entry, dict):
                continue
            algorithm = str(entry.get("algorithm") or "").lower()
            algorithms.append(algorithm)
            value = str(entry.get("value") or entry.get("signature") or "")
            try:
                if algorithm == "ed25519" and self._verify_ed25519(payload, value):
                    valid_count += 1
                elif algorithm == "sha256":
                    if hashlib.sha256(payload).hexdigest() == value:
                        valid_count += 1
            except Exception as exc:
                last_error = str(exc)[:200]
        return SignatureVerification(valid_count > 0, valid_count, algorithms, "" if valid_count else (last_error or "no signature matched release public key"))

    def _read_public_key(self) -> str:
        if self.public_key:
            return self.public_key
        if self.public_key_file and self.public_key_file.exists():
            return self.public_key_file.read_text(encoding="utf-8").strip()
        raise RuntimeError("release public key is not configured")

    def _verify_ed25519(self, payload: bytes, signature_value: str) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key_text = self._read_public_key()
        if key_text.startswith("base64:"):
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(key_text.split(":", 1)[1]))
        else:
            loaded_key = serialization.load_pem_public_key(key_text.encode("utf-8"))
            if not isinstance(loaded_key, ed25519.Ed25519PublicKey):
                raise RuntimeError("release public key must be Ed25519")
            public_key = loaded_key
        signature = base64.b64decode(signature_value.split(":", 1)[1] if signature_value.startswith("base64:") else signature_value)
        try:
            public_key.verify(signature, payload)
            return True
        except InvalidSignature:
            return False


class ReleaseChannelResolver:
    AUTO_ACTIVATE_CHANNELS = {"stable", "real-ga"}

    def evaluate(self, *, manifest: dict[str, Any], current_version: str, cfg: UpdateConfig, signature: SignatureVerification) -> UpdatePolicyDecision:
        if not cfg.enabled:
            return UpdatePolicyDecision(False, False, False, "updates are disabled", cfg.channel)
        version = str(manifest.get("version") or "")
        if not version:
            return UpdatePolicyDecision(False, False, False, "manifest version is missing", cfg.channel)
        if _version_tuple(version) <= _version_tuple(current_version):
            return UpdatePolicyDecision(False, False, False, "manifest is not newer than current version", cfg.channel)
        channel = str(manifest.get("release_channel") or manifest.get("channel") or cfg.channel)
        if cfg.require_signature and not signature.valid:
            return UpdatePolicyDecision(False, False, False, f"signature verification failed: {signature.reason}", channel)
        if channel == "emergency-hotfix" and signature.valid_count < 2:
            return UpdatePolicyDecision(True, True, False, "emergency-hotfix requires dual signatures before activation", channel)
        if channel not in self.AUTO_ACTIVATE_CHANNELS:
            return UpdatePolicyDecision(True, True, False, f"{channel} can be downloaded and staged only", channel)
        external = manifest.get("external_ga_evidence") or {}
        external_ok = external.get("status") == "passed" and int(external.get("blocker_count", 1) or 0) == 0
        if cfg.require_external_ga_evidence and not external_ok:
            return UpdatePolicyDecision(True, True, False, "external GA evidence is not passed; candidate may be staged only", channel)
        if not cfg.allow_auto_activate:
            return UpdatePolicyDecision(True, True, False, "auto activation is disabled by local policy", channel)
        current = _version_tuple(current_version)
        target = _version_tuple(version)
        if current[:2] == target[:2] and target[2] - current[2] > cfg.max_skipped_versions:
            return UpdatePolicyDecision(True, True, False, "too many skipped patch versions; manual review required", channel)
        return UpdatePolicyDecision(True, True, True, "auto activation allowed", channel)


class ArtifactDownloader:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def download(self, artifact: dict[str, Any], *, base_dir: Path | None = None) -> Path:
        rel = str(artifact.get("path") or "")
        if not rel:
            raise RuntimeError("artifact path is missing")
        target = self.cache_dir / Path(rel).name
        if rel.startswith("http://") or rel.startswith("https://"):
            with urllib.request.urlopen(rel, timeout=120) as response:  # nosec B310 - release artifact URL from signed manifest
                target.write_bytes(response.read())
        else:
            src = Path(rel[7:] if rel.startswith("file://") else rel)
            if not src.is_absolute() and base_dir is not None:
                src = base_dir / src
            shutil.copy2(src, target)
        expected = str(artifact.get("sha256") or "")
        if expected and sha256_file(target) != expected:
            target.unlink(missing_ok=True)
            raise RuntimeError("artifact sha256 mismatch")
        return target


class SBOMVerifier:
    def verify(self, *, manifest: dict[str, Any], artifact: dict[str, Any], base_dir: Path | None = None, require_sbom: bool = True) -> bool:
        sbom_path = artifact.get("sbom_path") or (manifest.get("sbom") or {}).get("path")
        sbom_sha = artifact.get("sbom_sha256") or (manifest.get("sbom") or {}).get("sha256")
        if not sbom_path:
            if require_sbom:
                raise RuntimeError("SBOM is required by local update policy")
            return False
        path = Path(str(sbom_path))
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        if not path.exists():
            raise RuntimeError("SBOM file is missing")
        if sbom_sha and sha256_file(path) != str(sbom_sha):
            raise RuntimeError("SBOM sha256 mismatch")
        if not manifest.get("source_commit"):
            raise RuntimeError("manifest source_commit is required")
        return True


class ReleaseSlotManager:
    def __init__(self, releases_dir: Path):
        self.releases_dir = Path(releases_dir).expanduser()
        self.releases_dir.mkdir(parents=True, exist_ok=True)

    def stage(self, *, version: str, artifact_path: Path, manifest: dict[str, Any]) -> Path:
        candidate_dir = self.releases_dir / f"{version}-candidate"
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        candidate_dir.mkdir(parents=True)
        if zipfile.is_zipfile(artifact_path):
            with zipfile.ZipFile(artifact_path) as zf:
                self._safe_extract_zip(zf, candidate_dir)
        else:
            shutil.copy2(artifact_path, candidate_dir / artifact_path.name)
        (candidate_dir / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._replace_symlink("candidate", candidate_dir)
        return candidate_dir

    def activate(self, candidate_dir: Path, *, health_check: Callable[[Path], bool] | None = None, rollback_on_failure: bool = True) -> bool:
        current = self.releases_dir / "current"
        previous_target = current.resolve() if current.is_symlink() or current.exists() else None
        if previous_target and previous_target.exists():
            self._replace_symlink("previous", previous_target)
        self._replace_symlink("current", candidate_dir)
        ok = bool(health_check(candidate_dir)) if health_check else True
        if not ok and rollback_on_failure and previous_target:
            self._replace_symlink("current", previous_target)
        return ok

    def rollback(self) -> bool:
        previous = self.releases_dir / "previous"
        if not previous.exists():
            return False
        self._replace_symlink("current", previous.resolve())
        return True

    def _replace_symlink(self, name: str, target: Path) -> None:
        link = self.releases_dir / name
        tmp = self.releases_dir / f".{name}.tmp"
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        os.symlink(str(target.resolve()), tmp)
        os.replace(tmp, link)

    @staticmethod
    def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
        root = target_dir.resolve()
        for member in zf.infolist():
            dest = (target_dir / member.filename).resolve()
            if root not in (dest, *dest.parents):
                raise RuntimeError("release artifact contains unsafe zip path")
        zf.extractall(target_dir)


class AutoUpdateRunner:
    def __init__(
        self,
        *,
        cfg: UpdateConfig,
        manifest_client: UpdateManifestClient,
        signature_verifier: SignatureVerifier,
        health_check: Callable[[Path], bool] | None = None,
        audit: UpdateAuditLog | None = None,
    ):
        self.cfg = cfg
        self.manifest_client = manifest_client
        self.signature_verifier = signature_verifier
        self.resolver = ReleaseChannelResolver()
        self.downloader = ArtifactDownloader(cfg.artifact_cache_dir)
        self.sbom = SBOMVerifier()
        self.slots = ReleaseSlotManager(cfg.release_slots_dir)
        self.health_check = health_check
        self.audit = audit or UpdateAuditLog(cfg.audit_log)

    def run_once(self, *, current_version: str) -> dict[str, Any]:
        bundle = self.manifest_client.fetch_latest()
        manifest = bundle.manifest
        signature = self.signature_verifier.verify_manifest(manifest)
        decision = self.resolver.evaluate(manifest=manifest, current_version=current_version, cfg=self.cfg, signature=signature)
        version = str(manifest.get("version") or "")
        self.audit.record("update.manifest_checked", to_version=version, manifest_sha256=bundle.sha256, signature_verified=signature.valid, channel=decision.channel, decision=decision.reason)
        if not decision.can_download:
            return {"ok": False, "status": "blocked", "reason": decision.reason}
        artifact = self._select_artifact(manifest)
        artifact_path = self.downloader.download(artifact, base_dir=bundle.base_dir)
        artifact_sha = sha256_file(artifact_path)
        sbom_verified = self.sbom.verify(manifest=manifest, artifact=artifact, base_dir=bundle.base_dir, require_sbom=self.cfg.require_sbom)
        if not decision.can_stage:
            return {"ok": False, "status": "downloaded", "reason": decision.reason, "artifact": str(artifact_path)}
        candidate_dir = self.slots.stage(version=version, artifact_path=artifact_path, manifest=manifest)
        self.audit.record("update.staged", to_version=version, manifest_sha256=bundle.sha256, artifact_sha256=artifact_sha, sbom_verified=sbom_verified, candidate_dir=str(candidate_dir), network_state="reconnected")
        if not decision.can_activate:
            return {"ok": True, "status": "staged", "reason": decision.reason, "candidate_dir": str(candidate_dir)}
        health_ok = self.slots.activate(candidate_dir, health_check=self.health_check if self.cfg.require_health_check else None, rollback_on_failure=self.cfg.rollback_on_failure)
        if not health_ok:
            self.audit.record("update.rollback", to_version=version, manifest_sha256=bundle.sha256, artifact_sha256=artifact_sha, health_check="failed", network_state="reconnected")
            return {"ok": False, "status": "rolled_back", "reason": "health check failed"}
        self.audit.record("update.activated", to_version=version, manifest_sha256=bundle.sha256, artifact_sha256=artifact_sha, signature_verified=signature.valid, sbom_verified=sbom_verified, health_check="passed", rollback_point=str((self.cfg.release_slots_dir / "previous").resolve()) if (self.cfg.release_slots_dir / "previous").exists() else "", actor="system", network_state="reconnected")
        return {"ok": True, "status": "activated", "version": version, "candidate_dir": str(candidate_dir)}

    @staticmethod
    def _select_artifact(manifest: dict[str, Any]) -> dict[str, Any]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise RuntimeError("manifest artifacts are missing")
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("required"):
                return artifact
        first = artifacts[0]
        if not isinstance(first, dict):
            raise RuntimeError("manifest artifact must be an object")
        return first
