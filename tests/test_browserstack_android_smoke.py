from __future__ import annotations

import base64
import json

from scripts.browserstack_android_smoke import (
    _copy_app_artifact,
    _multipart_form_data,
    _redact,
    _write_native_build_evidence,
)


def test_multipart_upload_body_includes_app_and_custom_id_without_credentials(tmp_path):
    app = tmp_path / "app-release.apk"
    app.write_bytes(b"apk-bytes")

    content_type, body = _multipart_form_data(
        fields={"custom_id": "omnidesk-android-smoke-123"},
        file_field="file",
        file_path=app,
    )

    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="custom_id"' in body
    assert b"omnidesk-android-smoke-123" in body
    assert b'filename="app-release.apk"' in body
    assert b"apk-bytes" in body
    assert b"browserstack-access-key" not in body


def test_redact_masks_access_key_and_basic_auth_token():
    username = "browserstack-user"
    access_key = "browserstack-access-key"
    encoded = base64.b64encode(f"{username}:{access_key}".encode("utf-8")).decode("ascii")

    message = f"{username}:{access_key} failed with Basic {encoded}"

    assert _redact(message, username=username, access_key=access_key) == "<redacted> failed with Basic <redacted>"


def test_native_build_evidence_references_copied_browserstack_apk(tmp_path):
    app = tmp_path / "app-release.apk"
    app.write_bytes(b"apk-bytes")
    evidence_dir = tmp_path / "release" / "external-evidence"

    artifact_rel, artifact_sha256 = _copy_app_artifact(app, evidence_dir)
    _write_native_build_evidence(
        evidence_dir,
        expected_version="1.12.7+root-monorepo-production-ga-candidate",
        producer="github-actions:owner/repo:123",
        artifact_rel=artifact_rel,
        artifact_sha256=artifact_sha256,
    )

    payload = json.loads((evidence_dir / "native-build/flutter-android-release.json").read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["command"] == "flutter build apk --release"
    assert payload["exit_code"] == 0
    assert payload["artifacts"] == [{"path": artifact_rel, "sha256": artifact_sha256}]
    assert "BrowserStack smoke input evidence" in payload["policy"]
    assert (evidence_dir / artifact_rel).read_bytes() == b"apk-bytes"
