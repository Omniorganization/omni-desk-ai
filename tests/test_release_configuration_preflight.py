from __future__ import annotations

from scripts.check_release_configuration import ALL_KNOWN_NAMES, main


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


def _set_complete_web_admin_config(monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_ADMIN_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_BASE_URL", "https://admin.example.test")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_API_BASE_URL", "https://admin.example.test/api")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_SESSION_PATH", "/api/smoke/session")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_APPROVAL_PATH", "/api/smoke/approval")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_AUDIT_PATH", "/api/smoke/audit")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_NOTIFICATION_PATH", "/api/smoke/notification")


def _set_complete_desktop_config(monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_DESKTOP_CLIENT_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_DESKTOP_API_BASE_URL", "https://desktop.example.test/api")
    monkeypatch.setenv("OMNIDESK_DESKTOP_SESSION_PATH", "/api/smoke/session")
    monkeypatch.setenv("OMNIDESK_DESKTOP_APPROVAL_PATH", "/api/smoke/approval")
    monkeypatch.setenv("OMNIDESK_DESKTOP_AUDIT_PATH", "/api/smoke/audit")
    monkeypatch.setenv("OMNIDESK_DESKTOP_NOTIFICATION_PATH", "/api/smoke/notification")
    monkeypatch.setenv("OMNIDESK_DESKTOP_UPDATE_CHANNEL", "stable")


def _set_complete_tri_app_smoke_config(monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_ADMIN_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_DESKTOP_CLIENT_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_MOBILE_CLIENT_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_BASE_URL", "https://admin.example.test")
    monkeypatch.setenv("OMNIDESK_DESKTOP_API_BASE_URL", "https://desktop.example.test/api")
    monkeypatch.setenv("OMNIDESK_MOBILE_API_BASE_URL", "https://mobile.example.test/api")
    monkeypatch.setenv("OMNIDESK_CHAIN_SESSION_PATH", "/api/smoke/session")
    monkeypatch.setenv("OMNIDESK_CHAIN_APPROVAL_PATH", "/api/smoke/approval")
    monkeypatch.setenv("OMNIDESK_CHAIN_AUDIT_PATH", "/api/smoke/audit")
    monkeypatch.setenv("OMNIDESK_CHAIN_NOTIFICATION_PATH", "/api/smoke/notification")


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


def test_web_admin_preflight_requires_specialized_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)

    assert main(["--scope", "web-admin"]) == 1
    captured = capsys.readouterr()
    assert "missing secret: OMNIDESK_WEB_ADMIN_ADMIN_TOKEN" in captured.err
    assert "missing var: OMNIDESK_WEB_ADMIN_BASE_URL" in captured.err
    assert "missing var: OMNIDESK_WEB_ADMIN_APPROVAL_PATH" in captured.err


def test_web_admin_preflight_rejects_noncanonical_paths(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_web_admin_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_AUDIT_PATH", "/api/../audit")

    assert main(["--scope", "web-admin"]) == 1
    assert "OMNIDESK_WEB_ADMIN_AUDIT_PATH" in capsys.readouterr().err


def test_desktop_preflight_accepts_complete_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_desktop_config(monkeypatch)

    assert main(["--scope", "desktop"]) == 0
    assert "desktop configuration preflight ok" in capsys.readouterr().out


def test_desktop_preflight_rejects_unknown_update_channel(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_desktop_config(monkeypatch)
    monkeypatch.setenv("OMNIDESK_DESKTOP_UPDATE_CHANNEL", "prod")

    assert main(["--scope", "desktop"]) == 1
    assert "OMNIDESK_DESKTOP_UPDATE_CHANNEL" in capsys.readouterr().err


def test_tri_app_smoke_preflight_requires_all_clients_and_chain_paths(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)

    assert main(["--scope", "tri-app-smoke"]) == 1
    captured = capsys.readouterr()
    assert "missing secret: OMNIDESK_WEB_ADMIN_ADMIN_TOKEN" in captured.err
    assert "missing secret: OMNIDESK_DESKTOP_CLIENT_TOKEN" in captured.err
    assert "missing secret: OMNIDESK_MOBILE_CLIENT_TOKEN" in captured.err
    assert "missing var: OMNIDESK_CHAIN_NOTIFICATION_PATH" in captured.err


def test_tri_app_smoke_preflight_accepts_complete_config(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    _set_complete_tri_app_smoke_config(monkeypatch)

    assert main(["--scope", "tri-app-smoke"]) == 0
    assert "tri-app-smoke configuration preflight ok" in capsys.readouterr().out
