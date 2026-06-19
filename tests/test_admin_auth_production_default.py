from __future__ import annotations

from omnidesk_agent.config import GatewayConfig
from omnidesk_agent.security.admin_auth import AdminAuth


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_gateway_admin_local_bypass_defaults_to_false():
    assert GatewayConfig().allow_local_admin_without_token is False


def test_admin_auth_can_enable_local_dev_bypass(monkeypatch):
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    auth = AdminAuth(allow_local_without_token=True)
    assert auth.verify_headers(Headers(), "127.0.0.1").ok
