from __future__ import annotations

from scripts.check_release_configuration import ALL_KNOWN_NAMES, main


def _clear_known_env(monkeypatch) -> None:
    for name in ALL_KNOWN_NAMES:
        monkeypatch.delenv(name, raising=False)


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

    assert main(["--scope", "release"]) == 0
    assert "release configuration preflight ok" in capsys.readouterr().out


def test_release_preflight_rejects_bad_sandbox_runner_digest(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
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
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_DIGEST", "not-a-digest")

    assert main(["--scope", "release"]) == 1
    assert "OMNIDESK_SANDBOX_RUNNER_DIGEST" in capsys.readouterr().err


def test_staging_preflight_requires_mode_specific_deploy_vars(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_BASE_URL", "https://staging.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_ADMIN_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_URL", "https://sandbox.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET", "set")

    assert main(["--scope", "staging", "--deploy-mode", "docker-compose", "--require-sandbox-smoke"]) == 1
    captured = capsys.readouterr()
    assert "missing var: OMNIDESK_DEPLOY_COMPOSE_FILE" in captured.err
    assert "missing var: OMNIDESK_DEPLOY_SERVICE" in captured.err


def test_production_preflight_forbids_noop(monkeypatch, capsys) -> None:
    _clear_known_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_RELEASE_SIGNING_KEY", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_BASE_URL", "https://prod.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_ADMIN_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_URL", "https://sandbox.example.test")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_TOKEN", "set")
    monkeypatch.setenv("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET", "set")

    assert main(["--scope", "production", "--deploy-mode", "noop", "--require-sandbox-smoke"]) == 1
    assert "production promotion must not use noop" in capsys.readouterr().err
