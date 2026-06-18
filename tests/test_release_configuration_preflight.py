from __future__ import annotations

import hashlib
import json

from scripts.check_release_configuration import ALL_KNOWN_NAMES, main
from scripts.import_ios_real_device_evidence import VERSION


def _clear_known_env(monkeypatch) -> None:
    for name in ALL_KNOWN_NAMES:
        monkeypatch.delenv(name, raising=False)


def _set_complete_release_config(monkeypatch) -> None:
    for name in [
        "OMNI_ANDROID_KEYSTORE_BASE64",
        "OMNI_ANDROID_KEYSTORE_PASSWORD",
        "OMNI_ANDROID_KEY_ALIAS",
        "OMNI_ANDROID_KEY_PASSWORD",
        "OMNI_ANDROID_GOOGLE_SERVICES_JSON",
        "OMNI_IOS_CERTIFICATE_P12_BASE64",
        "OMNI_IOS_CERTIFICATE_PASSWORD",
        "OMNI_IOS_PROVISIONING_PROFILE_BASE64",
        "OMNI_IOS_KEYCHAIN_PASSWORD",
        "OMNIDESK_RELEASE_SIGNING_KEY",
    ]:
        monkeypatch.setenv(name, "set")
    monkeypatch.setenv("OMNI_IOS_APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_DIGEST", "sha256:" + "a" * 64)


def _set_complete_downstream_config(monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_BASE_URL", "https://staging.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_ADMIN_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_URL", "https://sandbox.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET", "set")


def test_release_preflight_reports_missing_names_without_values(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    monkeypatch.setenv("OMNI_ANDROID_KEYSTORE_PASSWORD", "do-not-print-this-secret")

    assert main(["--scope", "release"]) == 1
    captured = capsys.readouterr()

    assert "missing secret: OMNI_ANDROID_KEYSTORE_BASE64" in captured.err
    assert "missing var: OMNI_IOS_APPLE_TEAM_ID" in captured.err
    assert "do-not-print-this-secret" not in captured.err


def test_release_preflight_accepts_complete_minimum_release_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_release_config(monkeypatch)

    assert main(["--scope", "release"]) == 0
    assert "release configuration preflight ok" in capsys.readouterr().out


def test_release_preflight_rejects_bad_sandbox_runner_digest(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_release_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_DIGEST", "not-a-digest")

    assert main(["--scope", "release"]) == 1
    assert "OMNIDESK_SANDBOX_RUNNER_DIGEST" in capsys.readouterr().err


def test_release_preflight_rejects_blank_or_whitespace_only_values(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_release_config(monkeypatch)
    monkeypatch.setenv("OMNI_ANDROID_KEY_ALIAS", "   ")

    assert main(["--scope", "release"]) == 1
    assert "missing secret: OMNI_ANDROID_KEY_ALIAS" in capsys.readouterr().err


def test_release_preflight_rejects_invalid_apple_team_id(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_release_config(monkeypatch)
    monkeypatch.setenv("OMNI_IOS_APPLE_TEAM_ID", "team")

    assert main(["--scope", "release"]) == 1
    assert "OMNI_IOS_APPLE_TEAM_ID" in capsys.readouterr().err


def test_staging_preflight_requires_mode_specific_deploy_vars(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)

    assert main(["--scope", "staging", "--deploy-mode", "docker-compose", "--require-sandbox-smoke"]) == 1
    captured = capsys.readouterr()
    assert "missing var: OMNIDESK_DEPLOY_COMPOSE_FILE" in captured.err
    assert "missing var: OMNIDESK_DEPLOY_SERVICE" in captured.err


def test_staging_preflight_rejects_url_without_host(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_SMOKE_BASE_URL", "https://")

    assert main(["--scope", "staging", "--deploy-mode", "noop", "--require-sandbox-smoke"]) == 1
    assert "OMNIDESK_SMOKE_BASE_URL" in capsys.readouterr().err


def test_staging_kubectl_preflight_requires_digest_pinned_image(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_KUBE_CONTEXT", "staging")
    monkeypatch.setenv("OMNIDESK_DEPLOY_NAMESPACE", "omnidesk")
    monkeypatch.setenv("OMNIDESK_DEPLOYMENT_NAME", "omnidesk-agent")
    monkeypatch.setenv("OMNIDESK_CONTAINER_NAME", "omnidesk")
    monkeypatch.setenv("OMNIDESK_IMAGE", "ghcr.io/example/omnidesk-agent:latest")

    assert main(["--scope", "staging", "--deploy-mode", "kubectl", "--require-sandbox-smoke"]) == 1
    assert "kubectl OMNIDESK_IMAGE must be pinned by digest" in capsys.readouterr().err


def test_systemd_preflight_accepts_default_remote_script(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_HOST", "deploy.example.test")
    monkeypatch.setenv("OMNIDESK_DEPLOY_USER", "omnidesk")

    assert main(["--scope", "staging", "--deploy-mode", "systemd", "--require-sandbox-smoke"]) == 0
    assert "staging configuration preflight ok" in capsys.readouterr().out


def test_systemd_preflight_rejects_remote_script_outside_usr_local_bin(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_HOST", "deploy.example.test")
    monkeypatch.setenv("OMNIDESK_DEPLOY_USER", "omnidesk")
    monkeypatch.setenv("OMNIDESK_REMOTE_DEPLOY_SCRIPT", "/tmp/deploy")

    assert main(["--scope", "staging", "--deploy-mode", "systemd", "--require-sandbox-smoke"]) == 1
    assert "OMNIDESK_REMOTE_DEPLOY_SCRIPT must be under /usr/local/bin" in capsys.readouterr().err


def test_production_preflight_forbids_noop(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)

    assert main(["--scope", "production", "--deploy-mode", "noop", "--require-sandbox-smoke"]) == 1
    assert "production promotion must not use noop" in capsys.readouterr().err


def test_kubectl_preflight_accepts_strict_digest_pinned_image(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_KUBE_CONTEXT", "staging")
    monkeypatch.setenv("OMNIDESK_DEPLOY_NAMESPACE", "omnidesk")
    monkeypatch.setenv("OMNIDESK_DEPLOYMENT_NAME", "omnidesk-agent")
    monkeypatch.setenv("OMNIDESK_CONTAINER_NAME", "omnidesk")
    monkeypatch.setenv("OMNIDESK_IMAGE", "ghcr.io/example/omnidesk-agent:1.11.3@sha256:" + "b" * 64)

    assert main(["--scope", "staging", "--deploy-mode", "kubectl", "--require-sandbox-smoke"]) == 0
    assert "staging configuration preflight ok" in capsys.readouterr().out


def test_kubectl_preflight_rejects_malformed_digest_image_with_extra_at(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_KUBE_CONTEXT", "staging")
    monkeypatch.setenv("OMNIDESK_DEPLOY_NAMESPACE", "omnidesk")
    monkeypatch.setenv("OMNIDESK_DEPLOYMENT_NAME", "omnidesk-agent")
    monkeypatch.setenv("OMNIDESK_CONTAINER_NAME", "omnidesk")
    monkeypatch.setenv("OMNIDESK_IMAGE", "ghcr.io/example/omnidesk@evil@sha256:" + "b" * 64)

    assert main(["--scope", "staging", "--deploy-mode", "kubectl", "--require-sandbox-smoke"]) == 1
    assert "kubectl OMNIDESK_IMAGE must be pinned by digest" in capsys.readouterr().err


def test_systemd_preflight_rejects_remote_script_path_traversal(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_downstream_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DEPLOY_HOST", "deploy.example.test")
    monkeypatch.setenv("OMNIDESK_DEPLOY_USER", "omnidesk")
    monkeypatch.setenv("OMNIDESK_REMOTE_DEPLOY_SCRIPT", "/usr/local/bin/../tmp/deploy")

    assert main(["--scope", "staging", "--deploy-mode", "systemd", "--require-sandbox-smoke"]) == 1
    assert "OMNIDESK_REMOTE_DEPLOY_SCRIPT must be a canonical path" in capsys.readouterr().err


def test_release_preflight_json_output_contains_structured_issues_without_secret_values(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    monkeypatch.setenv("OMNI_ANDROID_KEYSTORE_PASSWORD", "do-not-print-this-secret")

    assert main(["--scope", "release", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "do-not-print-this-secret" not in captured.err
    assert '"ok": false' in captured.err
    assert '"severity": "blocker"' in captured.err
    assert '"name": "OMNI_ANDROID_KEYSTORE_BASE64"' in captured.err


def test_release_preflight_writes_json_report(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_release_config(monkeypatch)
    report = tmp_path / "release-preflight.json"

    assert main(["--scope", "release", "--report-path", str(report)]) == 0
    assert "release configuration preflight ok" in capsys.readouterr().out
    data = report.read_text(encoding="utf-8")
    assert '"ok": true' in data
    assert '"issue_count": 0' in data


def _set_complete_web_admin_config(monkeypatch) -> None:
    monkeypatch.setenv("WEB_ADMIN_ADMIN_TOKEN", "set")
    monkeypatch.setenv("WEB_ADMIN_AUTH_SECRET", "set")
    monkeypatch.setenv("WEB_ADMIN_BASE_URL", "https://admin.example.test")
    monkeypatch.setenv("WEB_ADMIN_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("WEB_ADMIN_IMAGE", "ghcr.io/example/omnidesk-web-admin:1.11.4@sha256:" + "c" * 64)


def _set_complete_desktop_config(monkeypatch) -> None:
    monkeypatch.setenv("DESKTOP_BRIDGE_TOKEN", "set")
    monkeypatch.setenv("DESKTOP_BRIDGE_HMAC_SECRET", "set")
    monkeypatch.setenv("DESKTOP_AGENT_BASE_URL", "http://localhost:18789")
    monkeypatch.setenv("DESKTOP_UPDATE_ENDPOINT", "https://updates.example.test/desktop")
    monkeypatch.setenv("DESKTOP_APP_IDENTIFIER", "com.omnidesk.agent")
    monkeypatch.setenv("DESKTOP_BRIDGE_ORIGIN", "http://localhost:18789")


def _set_complete_mobile_config(monkeypatch) -> None:
    monkeypatch.setenv("MOBILE_APPROVAL_TOKEN", "set")
    monkeypatch.setenv("MOBILE_PUSH_HMAC_SECRET", "set")
    monkeypatch.setenv("MOBILE_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("MOBILE_APPROVAL_CALLBACK_URL", "https://api.example.test/mobile/approval/callback")
    monkeypatch.setenv("OMNI_ANDROID_PACKAGE_NAME", "com.omnidesk.mobile")
    monkeypatch.setenv("OMNI_IOS_BUNDLE_ID", "com.omnidesk.mobile")


def _set_complete_tri_app_config(monkeypatch) -> None:
    monkeypatch.setenv("TRI_APP_ADMIN_TOKEN", "set")
    monkeypatch.setenv("TRI_APP_MOBILE_APPROVAL_TOKEN", "set")
    monkeypatch.setenv("TRI_APP_DESKTOP_AGENT_TOKEN", "set")
    monkeypatch.setenv("TRI_APP_AUDIT_HMAC_SECRET", "set")
    monkeypatch.setenv("TRI_APP_BACKEND_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("TRI_APP_WEB_ADMIN_BASE_URL", "https://admin.example.test")
    monkeypatch.setenv("TRI_APP_MOBILE_CALLBACK_URL", "https://api.example.test/mobile/approval/callback")
    monkeypatch.setenv("TRI_APP_DESKTOP_AGENT_URL", "http://localhost:18789")
    monkeypatch.setenv("TRI_APP_ORG_ID", "org_demo_001")


def test_web_admin_preflight_accepts_complete_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_web_admin_config(monkeypatch)

    assert main(["--scope", "web-admin"]) == 0
    assert "web-admin configuration preflight ok" in capsys.readouterr().out


def test_web_admin_preflight_requires_digest_pinned_image(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_web_admin_config(monkeypatch)
    monkeypatch.setenv("WEB_ADMIN_IMAGE", "ghcr.io/example/omnidesk-web-admin:latest")

    assert main(["--scope", "web-admin"]) == 1
    assert "WEB_ADMIN_IMAGE must be pinned by digest" in capsys.readouterr().err


def test_web_admin_preflight_rejects_plain_http_public_url(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_web_admin_config(monkeypatch)
    monkeypatch.setenv("WEB_ADMIN_BASE_URL", "http://admin.example.test")

    assert main(["--scope", "web-admin"]) == 1
    assert "WEB_ADMIN_BASE_URL must be https or localhost http" in capsys.readouterr().err


def test_desktop_preflight_accepts_local_agent_and_https_update_endpoint(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_desktop_config(monkeypatch)

    assert main(["--scope", "desktop"]) == 0
    assert "desktop configuration preflight ok" in capsys.readouterr().out


def test_desktop_preflight_rejects_bridge_origin_with_path(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_desktop_config(monkeypatch)
    monkeypatch.setenv("DESKTOP_BRIDGE_ORIGIN", "http://localhost:18789/path")

    assert main(["--scope", "desktop"]) == 1
    assert "DESKTOP_BRIDGE_ORIGIN must be an https origin or localhost http origin without path/query" in capsys.readouterr().err


def test_desktop_preflight_rejects_invalid_app_identifier(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_desktop_config(monkeypatch)
    monkeypatch.setenv("DESKTOP_APP_IDENTIFIER", "omnidesk")

    assert main(["--scope", "desktop"]) == 1
    assert "DESKTOP_APP_IDENTIFIER" in capsys.readouterr().err


def test_mobile_preflight_accepts_complete_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_mobile_config(monkeypatch)

    assert main(["--scope", "mobile"]) == 0
    assert "mobile configuration preflight ok" in capsys.readouterr().out


def test_mobile_preflight_rejects_bad_android_package(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_mobile_config(monkeypatch)
    monkeypatch.setenv("OMNI_ANDROID_PACKAGE_NAME", "Com.Omnidesk.Mobile")

    assert main(["--scope", "mobile"]) == 1
    assert "OMNI_ANDROID_PACKAGE_NAME must be a lowercase Android package name" in capsys.readouterr().err


def test_mobile_preflight_rejects_bad_ios_bundle_id(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_mobile_config(monkeypatch)
    monkeypatch.setenv("OMNI_IOS_BUNDLE_ID", "omnidesk")

    assert main(["--scope", "mobile"]) == 1
    assert "OMNI_IOS_BUNDLE_ID" in capsys.readouterr().err


def test_tri_app_preflight_accepts_complete_roundtrip_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_config(monkeypatch)

    assert main(["--scope", "tri-app", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert '"ok": true' in captured.out
    assert '"scope": "tri-app"' in captured.out


def test_tri_app_preflight_requires_all_roundtrip_tokens(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_config(monkeypatch)
    monkeypatch.delenv("TRI_APP_DESKTOP_AGENT_TOKEN", raising=False)

    assert main(["--scope", "tri-app"]) == 1
    assert "missing secret: TRI_APP_DESKTOP_AGENT_TOKEN" in capsys.readouterr().err


def test_tri_app_preflight_rejects_public_http_backend(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_config(monkeypatch)
    monkeypatch.setenv("TRI_APP_BACKEND_BASE_URL", "http://api.example.test")

    assert main(["--scope", "tri-app"]) == 1
    assert "TRI_APP_BACKEND_BASE_URL must be https or localhost http" in capsys.readouterr().err


def test_tri_app_preflight_rejects_invalid_org_id(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_config(monkeypatch)
    monkeypatch.setenv("TRI_APP_ORG_ID", "!!")

    assert main(["--scope", "tri-app"]) == 1
    assert "TRI_APP_ORG_ID" in capsys.readouterr().err



def _write_complete_ios_evidence_tree(tmp_path):
    raw = tmp_path / "ios-evidence"
    ipa_artifact = raw / "artifacts" / "OmniDesk.ipa"
    ipa_artifact.parent.mkdir(parents=True, exist_ok=True)
    ipa_artifact.write_bytes(b"signed ipa artifact")
    ipa_digest = hashlib.sha256(b"signed ipa artifact").hexdigest()
    receipt_artifact = raw / "artifacts" / "ios" / "apns-delivery-receipt.json"
    receipt_artifact.parent.mkdir(parents=True, exist_ok=True)
    receipt_artifact.write_bytes(b'{"receipt":"accepted"}')
    receipt_digest = hashlib.sha256(b'{"receipt":"accepted"}').hexdigest()

    def write(rel: str, doc: dict) -> None:
        target = raw / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")

    base = {
        "schema_version": "ios-real-device-evidence/v1",
        "status": "verified",
        "produced_at": "2026-06-17T00:00:00Z",
        "producer": "device-lab",
        "platform": "ios",
        "version": VERSION,
        "bundle_id": "com.omnidesk.mobile",
    }
    write("native-build/flutter-ios-release.json", {
        **base,
        "command": "flutter build ipa --release",
        "exit_code": 0,
        "artifacts": [{"kind": "ios_unsigned_or_exported_ipa", "path": "artifacts/OmniDesk.ipa", "sha256": ipa_digest}],
        "smoke_cases": {"archive_created": True, "ipa_exported": True, "codesign_metadata_present": True},
    })
    write("signed-artifacts/ios-signed-ipa.json", {
        **base,
        "signature_verified": True,
        "source_native_artifact_sha256": ipa_digest,
        "device_udid_sha256": hashlib.sha256(b"device").hexdigest(),
        "artifacts": [{"kind": "ios_signed_ipa", "path": "artifacts/OmniDesk.ipa", "sha256": ipa_digest}],
        "smoke_cases": {
            "install_to_real_device": True,
            "launch_success": True,
            "gateway_connect": True,
            "device_enrollment": True,
            "mobile_chat": True,
            "approval_decision": True,
            "biometric_or_pin_confirm": True,
        },
    })
    write("push/apns-live-delivery.json", {
        **base,
        "provider": "apns",
        "delivery_success": True,
        "delivery_receipt_id": "apns-receipt-001",
        "push_token_sha256": hashlib.sha256(b"push-token").hexdigest(),
        "artifacts": [
            {
                "kind": "apns_provider_receipt",
                "path": "artifacts/ios/apns-delivery-receipt.json",
                "sha256": receipt_digest,
            }
        ],
        "smoke_cases": {
            "permission_requested": True,
            "token_registered_with_gateway": True,
            "provider_accepted_message": True,
            "device_received_notification": True,
        },
    })
    return raw


def test_ios_evidence_preflight_requires_raw_dir(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)

    assert main(["--scope", "ios-evidence"]) == 1
    assert "missing var: IOS_EVIDENCE_RAW_DIR" in capsys.readouterr().err


def test_ios_evidence_preflight_accepts_complete_raw_tree(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    raw = _write_complete_ios_evidence_tree(tmp_path)
    monkeypatch.setenv("IOS_EVIDENCE_RAW_DIR", str(raw))
    monkeypatch.setenv("IOS_EVIDENCE_EXPECTED_VERSION", VERSION)

    assert main(["--scope", "ios-evidence"]) == 0
    assert "ios-evidence configuration preflight ok" in capsys.readouterr().out


def test_ios_evidence_preflight_rejects_missing_required_file(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    raw = _write_complete_ios_evidence_tree(tmp_path)
    (raw / "push/apns-live-delivery.json").unlink()
    monkeypatch.setenv("IOS_EVIDENCE_RAW_DIR", str(raw))
    monkeypatch.setenv("IOS_EVIDENCE_EXPECTED_VERSION", VERSION)

    assert main(["--scope", "ios-evidence"]) == 1
    assert "push/apns-live-delivery.json" in capsys.readouterr().err


def _set_complete_mobile_real_device_config(monkeypatch, tmp_path) -> None:
    raw = _write_complete_ios_evidence_tree(tmp_path)
    monkeypatch.setenv("IOS_EVIDENCE_RAW_DIR", str(raw))
    monkeypatch.setenv("IOS_EVIDENCE_EXPECTED_VERSION", VERSION)
    monkeypatch.setenv("IOS_DEVICE_UDID", "00008110-001234567890801E")
    monkeypatch.setenv("IOS_DEVICE_NAME", "Yufan iPhone")
    monkeypatch.setenv("IOS_SIGNED_IPA_PATH", "dist/OmniDesk.ipa")
    monkeypatch.setenv("MOBILE_APPROVAL_TOKEN", "set")
    monkeypatch.setenv("MOBILE_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("MOBILE_APPROVAL_CALLBACK_URL", "https://api.example.test/mobile/approval/callback")
    monkeypatch.setenv("OMNI_IOS_BUNDLE_ID", "com.omnidesk.mobile")


def test_mobile_real_device_preflight_accepts_complete_config(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_mobile_real_device_config(monkeypatch, tmp_path)

    assert main(["--scope", "mobile-real-device"]) == 0
    assert "mobile-real-device configuration preflight ok" in capsys.readouterr().out


def test_mobile_real_device_preflight_rejects_non_ipa_path(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_mobile_real_device_config(monkeypatch, tmp_path)
    monkeypatch.setenv("IOS_SIGNED_IPA_PATH", "dist/OmniDesk.zip")

    assert main(["--scope", "mobile-real-device"]) == 1
    assert "IOS_SIGNED_IPA_PATH must point to a signed .ipa" in capsys.readouterr().err


def _write_tri_app_live_smoke_report(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "tri-app-live-smoke/v1",
        "status": "passed",
        "scenario_id": "approval-roundtrip-001",
        "org_id": "org_demo_001",
        "trace_id": "trace-001",
        "started_at": "2026-06-17T00:00:00Z",
        "finished_at": "2026-06-17T00:00:05Z",
        "latency_ms": 5000,
        "steps": {
            "desktop_action_proposed": True,
            "backend_approval_created": True,
            "mobile_push_received": True,
            "mobile_approval_decision_submitted": True,
            "desktop_action_resumed": True,
            "audit_event_written": True,
            "web_admin_audit_visible": True,
        },
    }), encoding="utf-8")


def _set_complete_tri_app_live_smoke_config(monkeypatch, tmp_path) -> None:
    _set_complete_tri_app_config(monkeypatch)
    report = tmp_path / "dist" / "tri-app-live-smoke.json"
    _write_tri_app_live_smoke_report(report)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRI_APP_LIVE_SMOKE_SCENARIO_ID", "approval-roundtrip-001")
    monkeypatch.setenv("TRI_APP_LIVE_SMOKE_REPORT_PATH", "dist/tri-app-live-smoke.json")


def test_tri_app_live_smoke_preflight_accepts_complete_config(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_live_smoke_config(monkeypatch, tmp_path)

    assert main(["--scope", "tri-app-live-smoke", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert '"scope": "tri-app-live-smoke"' in captured.out
    assert '"ok": true' in captured.out


def test_tri_app_live_smoke_preflight_rejects_unsafe_report_path(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_live_smoke_config(monkeypatch, tmp_path)
    monkeypatch.setenv("TRI_APP_LIVE_SMOKE_REPORT_PATH", "../tri-app-live-smoke.json")

    assert main(["--scope", "tri-app-live-smoke"]) == 1
    assert "TRI_APP_LIVE_SMOKE_REPORT_PATH must be a safe relative report path" in capsys.readouterr().err


def test_tri_app_live_smoke_preflight_rejects_failed_report(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_live_smoke_config(monkeypatch, tmp_path)
    report = tmp_path / "dist" / "tri-app-live-smoke.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    data["steps"]["web_admin_audit_visible"] = False
    report.write_text(json.dumps(data), encoding="utf-8")

    assert main(["--scope", "tri-app-live-smoke"]) == 1
    assert "steps.web_admin_audit_visible must be true" in capsys.readouterr().err


def test_ios_evidence_preflight_rejects_semantically_invalid_tree(monkeypatch, tmp_path, capsys) -> None:
    _clear_known_env(monkeypatch)
    raw = _write_complete_ios_evidence_tree(tmp_path)
    doc = json.loads((raw / "signed-artifacts/ios-signed-ipa.json").read_text(encoding="utf-8"))
    doc["smoke_cases"]["launch_success"] = False
    (raw / "signed-artifacts/ios-signed-ipa.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("IOS_EVIDENCE_RAW_DIR", str(raw))

    assert main(["--scope", "ios-evidence"]) == 1
    assert "launch_success" in capsys.readouterr().err
