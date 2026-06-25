from __future__ import annotations

import base64
import json
import os
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from omnidesk_agent.config import UpdateConfig
from omnidesk_agent.self_upgrade.auto_update import (
    AutoUpdateRunner,
    SignatureVerifier,
    UpdateManifestClient,
    sha256_file,
)


def _write_signed_manifest(tmp_path: Path, *, external_passed: bool = True, version: str = "1.12.8") -> tuple[Path, str]:
    artifact = tmp_path / "omnidesk.zip"
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "VERSION").write_text(version, encoding="utf-8")
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.write(payload_dir / "VERSION", "VERSION")
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"packages": [{"name": "omnidesk-agent", "version": version}]}), encoding="utf-8")

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    manifest = {
        "version": version,
        "native_version": version,
        "package_slug": "omnidesk",
        "release_channel": "stable",
        "release_status": "customer_distribution_ga" if external_passed else "source_gated_candidate",
        "source_commit": "abc123",
        "artifacts": [
            {
                "kind": "backend",
                "path": artifact.name,
                "bytes": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
                "sbom_path": sbom.name,
                "sbom_sha256": sha256_file(sbom),
                "required": True,
            }
        ],
        "external_ga_evidence": {
            "status": "passed" if external_passed else "blocked_missing_external_evidence",
            "blocker_count": 0 if external_passed else 7,
        },
        "policy": {"auto_activate": True},
    }
    signature = private_key.sign(SignatureVerifier.canonical_payload(manifest))
    manifest["signature"] = {"algorithm": "ed25519", "value": base64.b64encode(signature).decode("ascii")}
    manifest_path = tmp_path / "latest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest_path, public_key


def _cfg(tmp_path: Path, manifest_path: Path) -> UpdateConfig:
    return UpdateConfig(
        manifest_url=str(manifest_path),
        release_slots_dir=tmp_path / "releases",
        artifact_cache_dir=tmp_path / "cache",
        audit_log=tmp_path / "update_audit.jsonl",
    )


def test_auto_update_verifies_signed_manifest_stages_and_activates(tmp_path: Path):
    manifest_path, public_key = _write_signed_manifest(tmp_path)
    cfg = _cfg(tmp_path, manifest_path)
    runner = AutoUpdateRunner(
        cfg=cfg,
        manifest_client=UpdateManifestClient(str(manifest_path)),
        signature_verifier=SignatureVerifier(public_key=public_key),
        health_check=lambda candidate: (candidate / "VERSION").read_text(encoding="utf-8") == "1.12.8",
    )

    result = runner.run_once(current_version="1.12.7")

    assert result["status"] == "activated"
    assert (cfg.release_slots_dir / "current").resolve().name == "1.12.8-candidate"
    audit = cfg.audit_log.read_text(encoding="utf-8")
    assert "update.activated" in audit
    assert "signature_verified" in audit


def test_auto_update_stages_only_when_external_ga_evidence_is_blocked(tmp_path: Path):
    manifest_path, public_key = _write_signed_manifest(tmp_path, external_passed=False)
    cfg = _cfg(tmp_path, manifest_path)
    runner = AutoUpdateRunner(
        cfg=cfg,
        manifest_client=UpdateManifestClient(str(manifest_path)),
        signature_verifier=SignatureVerifier(public_key=public_key),
        health_check=lambda _candidate: True,
    )

    result = runner.run_once(current_version="1.12.7")

    assert result["status"] == "staged"
    assert "external GA evidence is not passed" in result["reason"]
    assert not (cfg.release_slots_dir / "current").exists()


def test_auto_update_rolls_back_when_health_check_fails(tmp_path: Path):
    manifest_path, public_key = _write_signed_manifest(tmp_path)
    cfg = _cfg(tmp_path, manifest_path)
    previous = cfg.release_slots_dir / "1.12.7"
    previous.mkdir(parents=True)
    (previous / "VERSION").write_text("1.12.7", encoding="utf-8")
    os.symlink(str(previous), cfg.release_slots_dir / "current")
    runner = AutoUpdateRunner(
        cfg=cfg,
        manifest_client=UpdateManifestClient(str(manifest_path)),
        signature_verifier=SignatureVerifier(public_key=public_key),
        health_check=lambda _candidate: False,
    )

    result = runner.run_once(current_version="1.12.7")

    assert result["status"] == "rolled_back"
    assert (cfg.release_slots_dir / "current").resolve() == previous.resolve()
    assert "update.rollback" in cfg.audit_log.read_text(encoding="utf-8")
