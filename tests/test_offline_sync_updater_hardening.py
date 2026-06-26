from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from omnidesk_agent.appsync.offline_sync_apply import patch_offline_sync_application
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.config import UpdateConfig
from omnidesk_agent.self_upgrade.auto_update import (
    AutoUpdateRunner,
    SignatureVerifier,
    UpdateManifestClient,
    default_release_health_check,
    sha256_file,
)


def _signed_manifest(tmp_path: Path, *, include_artifact_hash: bool = True) -> tuple[Path, str]:
    artifact = tmp_path / "omnidesk.zip"
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "VERSION").write_text("1.12.8", encoding="utf-8")
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.write(payload_dir / "VERSION", "VERSION")
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"packages": [{"name": "omnidesk-agent", "version": "1.12.8"}]}), encoding="utf-8")

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    artifact_item = {
        "kind": "backend",
        "path": artifact.name,
        "bytes": artifact.stat().st_size,
        "sbom_path": sbom.name,
        "sbom_sha256": sha256_file(sbom),
        "required": True,
    }
    if include_artifact_hash:
        artifact_item["sha256"] = sha256_file(artifact)

    manifest = {
        "version": "1.12.8",
        "release_channel": "stable",
        "release_status": "customer_distribution_ga",
        "source_commit": "abc123",
        "artifacts": [artifact_item],
        "external_ga_evidence": {"status": "passed", "blocker_count": 0},
    }
    signature = private_key.sign(SignatureVerifier.canonical_payload(manifest))
    manifest["signature"] = {"algorithm": "ed25519", "value": base64.b64encode(signature).decode("ascii")}
    manifest_path = tmp_path / "latest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest_path, public_key


def test_update_manifest_rejects_plain_sha256_pseudo_signature() -> None:
    manifest = {"version": "1.12.8", "artifacts": []}
    manifest["signature"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(SignatureVerifier.canonical_payload(manifest)).hexdigest(),
    }

    result = SignatureVerifier().verify_manifest(manifest)

    assert not result.valid
    assert result.valid_count == 0
    assert result.algorithms == ["sha256"]
    assert "unsupported manifest signature algorithm" in result.reason


def test_update_runner_requires_artifact_sha256(tmp_path: Path) -> None:
    manifest_path, public_key = _signed_manifest(tmp_path, include_artifact_hash=False)
    cfg = UpdateConfig(
        manifest_url=str(manifest_path),
        release_slots_dir=tmp_path / "releases",
        artifact_cache_dir=tmp_path / "cache",
        audit_log=tmp_path / "audit.jsonl",
    )
    runner = AutoUpdateRunner(
        cfg=cfg,
        manifest_client=UpdateManifestClient(str(manifest_path)),
        signature_verifier=SignatureVerifier(public_key=public_key),
        health_check=default_release_health_check,
    )

    with pytest.raises(RuntimeError, match="artifact sha256 is required"):
        runner.run_once(current_version="1.12.7")


def test_update_runner_honors_disabled_background_download(tmp_path: Path) -> None:
    manifest_path, public_key = _signed_manifest(tmp_path)
    cfg = UpdateConfig(
        manifest_url=str(manifest_path),
        release_slots_dir=tmp_path / "releases",
        artifact_cache_dir=tmp_path / "cache",
        audit_log=tmp_path / "audit.jsonl",
        allow_background_download=False,
    )
    runner = AutoUpdateRunner(
        cfg=cfg,
        manifest_client=UpdateManifestClient(str(manifest_path)),
        signature_verifier=SignatureVerifier(public_key=public_key),
        health_check=default_release_health_check,
    )

    result = runner.run_once(current_version="1.12.7")

    assert result["status"] == "blocked"
    assert "background artifact download is disabled" in result["reason"]
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


def test_default_release_health_check_rejects_manifest_only_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "release-manifest.json").write_text(json.dumps({"version": "1.12.8"}), encoding="utf-8")

    assert default_release_health_check(candidate) is False

    (candidate / "VERSION").write_text("1.12.8", encoding="utf-8")
    assert default_release_health_check(candidate) is True


def test_uploaded_offline_operations_mutate_appsync_state(tmp_path: Path) -> None:
    patch_offline_sync_application()
    store = AppSyncStore(tmp_path / "appsync.json")
    operations = [
        {
            "operation_id": "op-conv",
            "operation_type": "conversation.created",
            "idempotency_key": "offline-conv",
            "payload": {"conversation_id": "conv_offline", "title": "Offline customer case"},
        },
        {
            "operation_id": "op-msg",
            "operation_type": "message.created",
            "idempotency_key": "offline-msg",
            "payload": {
                "message_id": "msg_offline",
                "conversation_id": "conv_offline",
                "role": "user",
                "content": "Captured while offline",
            },
        },
    ]

    result = store.receive_outbox_operations(actor="alice", operations=operations, remote="mobile", device_id="mobile-1")

    assert result["applied"] == 2
    assert store.conversations["conv_offline"].title == "Offline customer case"
    assert store.messages["msg_offline"].content == "Captured while offline"
    sync = store.sync_since(0, actor="alice")
    assert any(event["event_type"] == "conversation.created" for event in sync["events"])
    assert any(event["event_type"] == "message.created" for event in sync["events"])
