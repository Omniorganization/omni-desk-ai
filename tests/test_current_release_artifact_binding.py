from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.check_current_release_artifact_binding import PLATFORM_SPECS, audit


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    artifact_root = tmp_path / "artifacts"
    evidence_root = tmp_path / "evidence"
    extensions = {"android": ".aab", "ios": ".ipa", "macos": ".dmg", "windows": ".msi"}
    main_rows = []
    for spec in PLATFORM_SPECS:
        platform = str(spec["platform"])
        rel = f"bundle/omnidesk-{platform}{extensions[platform]}"
        data = f"signed-{platform}".encode()
        digest = _digest(data)
        platform_root = artifact_root / str(spec["artifact_directory"])
        artifact = platform_root / "payload" / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(data)
        _write_json(
            platform_root / "native-artifact-manifest.json",
            {
                "schema": "omnidesk-native-artifact-set/v1",
                "status": "passed",
                "source_commit": "abc1234",
                "build_run_id": "release-99",
                "artifacts": [{"path": rel, "sha256": digest, "release_payload_artifact_sha256": digest}],
            },
        )
        signature_fields = {field: f"value-{field}" for field in spec["signature_fields"]}
        signed = {
            "schema": "omnidesk-signed-artifact-evidence/v1",
            "status": "passed",
            "source_commit": "abc1234",
            "signing_run_id": f"sign-{platform}",
            "artifacts": [
                {
                    "path": rel,
                    "sha256": digest,
                    "signed_artifact_sha256": digest,
                    "native_signed_binding_sha256": digest,
                    "artifact_attestation": {"attestation_id": f"att-{platform}", "subject_sha256": digest},
                }
            ],
            **signature_fields,
            **{field: True for field in spec["true_fields"]},
        }
        _write_json(evidence_root / str(spec["signed_evidence"]), signed)
        main_rows.append(
            {
                "platform": platform,
                "digests_match": True,
                "valid": True,
                "source_commit": "abc1234",
                "build_run_id": f"native-{platform}",
                "signing_run_id": f"sign-{platform}",
                "signature_metadata": signature_fields,
                "missing_signature_fields": [],
                "failed_verifications": [],
                "artifacts": [
                    {
                        "path": rel,
                        "release_payload_artifact_sha256": digest,
                        "external_evidence_signed_artifact_sha256": digest,
                        "native_signed_binding_sha256": digest,
                        "artifact_attestation": {"attestation_id": f"att-{platform}", "subject_sha256": digest},
                        "source_commit": "abc1234",
                        "build_run_id": f"native-{platform}",
                        "signing_run_id": f"sign-{platform}",
                        "main_verification_run_id": "main-42",
                        "valid": True,
                    }
                ],
            }
        )
    _write_json(
        evidence_root / "control-plane/native-signed-artifact-binding.json",
        {
            "schema": "omnidesk-native-signed-artifact-binding/v1",
            "status": "passed",
            "repository": "Omniorganization/omni-desk-ai",
            "main_verification_commit": "abc1234",
            "main_verification_run_id": "main-42",
            "artifact_digest_bindings": main_rows,
        },
    )
    return artifact_root, evidence_root


def _audit(tmp_path: Path):
    artifact_root, evidence_root = _fixture(tmp_path)
    return audit(
        artifact_root=artifact_root,
        evidence_root=evidence_root,
        repository="Omniorganization/omni-desk-ai",
        source_commit="abc1234",
        release_run_id="release-99",
        main_verification_run_id="main-42",
    )


def test_current_release_artifacts_are_bound_per_digest(tmp_path: Path) -> None:
    report = _audit(tmp_path)

    assert report["status"] == "passed"
    assert report["all_artifacts_bound"] is True
    assert len(report["platforms"]) == 4
    assert all(platform["status"] == "passed" for platform in report["platforms"])


def test_current_release_rehash_blocks_modified_payload(tmp_path: Path) -> None:
    artifact_root, evidence_root = _fixture(tmp_path)
    target = next((artifact_root / "release-mobile-android/payload").rglob("*.aab"))
    target.write_bytes(b"tampered")

    report = audit(
        artifact_root=artifact_root,
        evidence_root=evidence_root,
        repository="Omniorganization/omni-desk-ai",
        source_commit="abc1234",
        release_run_id="release-99",
        main_verification_run_id="main-42",
    )

    assert report["status"] == "blocked"
    assert any("manifest digest mismatch" in failure for failure in report["failures"])


def test_current_release_blocks_unlisted_distributable_file(tmp_path: Path) -> None:
    artifact_root, evidence_root = _fixture(tmp_path)
    extra = artifact_root / "release-desktop-windows/payload/extra.exe"
    extra.write_bytes(b"unlisted")

    report = audit(
        artifact_root=artifact_root,
        evidence_root=evidence_root,
        repository="Omniorganization/omni-desk-ai",
        source_commit="abc1234",
        release_run_id="release-99",
        main_verification_run_id="main-42",
    )

    assert report["status"] == "blocked"
    assert any("do not exactly match the manifest" in failure for failure in report["failures"])


def test_current_release_blocks_wrong_build_and_main_verification_runs(tmp_path: Path) -> None:
    artifact_root, evidence_root = _fixture(tmp_path)

    report = audit(
        artifact_root=artifact_root,
        evidence_root=evidence_root,
        repository="Omniorganization/omni-desk-ai",
        source_commit="abc1234",
        release_run_id="different-release",
        main_verification_run_id="different-main",
    )

    assert report["status"] == "blocked"
    assert any("build_run_id" in failure for failure in report["failures"])
    assert any("selected evidence run" in failure for failure in report["failures"])


def test_current_release_blocks_missing_per_artifact_attestation(tmp_path: Path) -> None:
    artifact_root, evidence_root = _fixture(tmp_path)
    path = evidence_root / "signed-artifacts/ios-signed-ipa.json"
    signed = json.loads(path.read_text(encoding="utf-8"))
    signed["artifacts"][0].pop("artifact_attestation")
    _write_json(path, signed)

    report = audit(
        artifact_root=artifact_root,
        evidence_root=evidence_root,
        repository="Omniorganization/omni-desk-ai",
        source_commit="abc1234",
        release_run_id="release-99",
        main_verification_run_id="main-42",
    )

    assert report["status"] == "blocked"
    assert any("attestation id" in failure for failure in report["failures"])
