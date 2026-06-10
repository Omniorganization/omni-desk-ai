from __future__ import annotations
from omnidesk_agent.security.admin_auth import AdminAuth


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_admin_auth_allows_local_without_token(monkeypatch):
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    auth = AdminAuth(allow_local_without_token=True)
    assert auth.verify_headers(Headers(), "127.0.0.1").ok


def test_admin_auth_requires_token_for_remote(monkeypatch):
    monkeypatch.setenv("OMNIDESK_ADMIN_TOKEN", "secret")
    auth = AdminAuth()
    assert not auth.verify_headers(Headers(), "8.8.8.8").ok
    assert auth.verify_headers(Headers({"authorization": "Bearer secret"}), "8.8.8.8").ok
