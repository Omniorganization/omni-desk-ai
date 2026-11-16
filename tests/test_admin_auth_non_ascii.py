from __future__ import annotations

import json

import pytest

from omnidesk_agent.security.admin_auth import AdminAuth

ROLE_ENVS = ("OMNIDESK_ADMIN_TOKEN", "OMNIDESK_VIEWER_TOKEN", "OMNIDESK_OPERATOR_TOKEN", "OMNIDESK_OWNER_TOKEN")
OWNER_TOKEN = "o" * 40
LEGACY_SECRET = "l" * 40
NON_ASCII = "é" * 40


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def _clear_admin_env(monkeypatch):
    for name in ROLE_ENVS:
        monkeypatch.delenv(name, raising=False)


def _audit_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("headers", [
    {"authorization": "Bearer " + NON_ASCII},
    {"x-omnidesk-admin-token": NON_ASCII},
    # Starlette decodes raw header bytes as latin-1, so UTF-8 bytes arrive as mojibake.
    {"authorization": "Bearer " + "é".encode("utf-8").decode("latin-1") * 20},
])
def test_non_ascii_role_token_is_an_audited_denial(monkeypatch, tmp_path, headers):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    audit = tmp_path / "audit.jsonl"
    auth = AdminAuth(audit_log=audit)

    decision = auth.verify_headers(Headers(headers), "8.8.8.8", required_role="viewer", path="/admin/status")

    assert not decision.ok
    assert decision.reason == "missing or invalid admin token"
    events = _audit_events(audit)
    assert len(events) == 1
    assert events[0]["ok"] is False
    assert events[0]["reason"] == "missing or invalid admin token"
    assert events[0]["path"] == "/admin/status"


def test_non_ascii_legacy_secret_falls_through_to_audited_denial(monkeypatch, tmp_path):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("OMNIDESK_TEST_LEGACY_SECRET", LEGACY_SECRET)
    audit = tmp_path / "audit.jsonl"
    auth = AdminAuth(legacy_secret_env="OMNIDESK_TEST_LEGACY_SECRET", audit_log=audit)

    decision = auth.verify_headers(Headers({"x-omnidesk-gateway-secret": NON_ASCII}), "8.8.8.8", required_role="owner")

    assert not decision.ok
    assert decision.reason == "missing or invalid admin token"
    assert [event["ok"] for event in _audit_events(audit)] == [False]


def test_non_ascii_legacy_only_secret_is_an_audited_denial(monkeypatch, tmp_path):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_TEST_LEGACY_SECRET", LEGACY_SECRET)
    audit = tmp_path / "audit.jsonl"
    auth = AdminAuth(legacy_secret_env="OMNIDESK_TEST_LEGACY_SECRET", audit_log=audit)

    decision = auth.verify_headers(Headers({"x-omnidesk-gateway-secret": NON_ASCII}), "8.8.8.8", required_role="owner")

    assert not decision.ok
    assert decision.reason == "missing or invalid legacy gateway secret"
    assert [event["ok"] for event in _audit_events(audit)] == [False]


def test_non_ascii_configured_token_does_not_break_other_credentials(monkeypatch, tmp_path):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", NON_ASCII)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    auth = AdminAuth(audit_log=tmp_path / "audit.jsonl")

    accepted = auth.verify_headers(Headers({"authorization": "Bearer " + OWNER_TOKEN}), "8.8.8.8", required_role="owner")
    denied = auth.verify_headers(Headers({"authorization": "Bearer " + "x" * 40}), "8.8.8.8")

    assert accepted.ok and accepted.role == "owner"
    assert not denied.ok and denied.reason == "missing or invalid admin token"


def test_byte_comparison_keeps_exact_match_semantics(monkeypatch, tmp_path):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", NON_ASCII)
    auth = AdminAuth(audit_log=tmp_path / "audit.jsonl")

    exact = auth.verify_headers(Headers({"x-omnidesk-admin-token": NON_ASCII}), "8.8.8.8", required_role="operator")
    near = auth.verify_headers(Headers({"x-omnidesk-admin-token": NON_ASCII[:-1] + "e"}), "8.8.8.8", required_role="operator")
    lone_surrogate = auth.verify_headers(Headers({"x-omnidesk-admin-token": "\udcff" * 40}), "8.8.8.8", required_role="operator")

    assert exact.ok and exact.role == "operator"
    assert not near.ok
    assert not lone_surrogate.ok


def _http_config(tmp_path, *, legacy: bool = False):
    from omnidesk_agent.config import AppConfig

    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.gateway.allow_local_admin_without_token = False
    cfg.gateway.allow_legacy_gateway_secret_auth = legacy
    cfg.gateway.admin_allowed_ips = ["127.0.0.1", "testclient"]
    cfg.plugins.enabled = False
    cfg.ensure_dirs()
    return cfg


@pytest.mark.parametrize("header_name,prefix,legacy", [
    ("authorization", b"Bearer ", False),
    ("x-omnidesk-admin-token", b"", False),
    ("x-omnidesk-gateway-secret", b"", True),
])
def test_http_non_ascii_admin_credential_is_rejected_like_any_bad_token(monkeypatch, tmp_path, header_name, prefix, legacy):
    from fastapi.testclient import TestClient

    from omnidesk_agent.server import create_app

    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", LEGACY_SECRET)
    cfg = _http_config(tmp_path, legacy=legacy)
    app = create_app(cfg)
    audit = tmp_path / "admin_auth_audit.jsonl"

    with TestClient(app, raise_server_exceptions=False) as client:
        ascii_bad = client.get("/admin/status", headers={header_name: prefix + b"x" * 40})
        non_ascii = client.get("/admin/status", headers={header_name: prefix + NON_ASCII.encode("utf-8")})
        owner = client.get("/admin/status", headers={"authorization": "Bearer " + OWNER_TOKEN})

    assert ascii_bad.status_code == 403
    assert non_ascii.status_code == ascii_bad.status_code
    assert non_ascii.json() == ascii_bad.json() == {"detail": "missing or invalid admin token"}
    assert owner.status_code == 200
    events = [event for event in _audit_events(audit) if event["path"] == "/admin/status"]
    assert [event["ok"] for event in events] == [False, False, True]
    assert NON_ASCII not in audit.read_text(encoding="utf-8")
