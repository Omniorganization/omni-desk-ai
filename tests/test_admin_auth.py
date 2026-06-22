from __future__ import annotations
from omnidesk_agent.security.admin_auth import AdminAuth


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def _clear_admin_env(monkeypatch):
    for name in ("OMNIDESK_ADMIN_TOKEN", "OMNIDESK_VIEWER_TOKEN", "OMNIDESK_OPERATOR_TOKEN", "OMNIDESK_OWNER_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_admin_auth_allows_local_without_token(monkeypatch):
    _clear_admin_env(monkeypatch)
    auth = AdminAuth(allow_local_without_token=True)
    assert auth.verify_headers(Headers(), "127.0.0.1").ok


def test_admin_auth_requires_token_for_remote(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "secret")
    auth = AdminAuth()
    assert not auth.verify_headers(Headers(), "8.8.8.8").ok
    assert auth.verify_headers(Headers({"authorization": "Bearer secret"}), "8.8.8.8").ok


def test_admin_auth_ignores_client_declared_role(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "view")
    auth = AdminAuth()
    headers = Headers({"authorization": "Bearer view", "x-omnidesk-admin-role": "owner"})
    decision = auth.verify_headers(headers, "8.8.8.8", required_role="owner")
    assert not decision.ok
    assert decision.role == "viewer"


def test_admin_auth_binds_actor_to_verified_token_not_header(monkeypatch):
    _clear_admin_env(monkeypatch)
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "op")
    auth = AdminAuth()

    spoofed = auth.verify_headers(Headers({"authorization": "Bearer op", "x-omnidesk-actor": "system"}), "8.8.8.8", required_role="operator")

    assert spoofed.ok
    assert spoofed.actor == "token:OMNIDESK_OPERATOR_TOKEN"

    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    mapped = auth.verify_headers(Headers({"authorization": "Bearer op", "x-omnidesk-actor": "system"}), "8.8.8.8", required_role="operator")
    assert mapped.ok
    assert mapped.actor == "alice"
