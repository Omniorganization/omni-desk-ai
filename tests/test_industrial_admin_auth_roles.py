from __future__ import annotations


from omnidesk_agent.security.admin_auth import AdminAuth


class Headers(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def test_admin_auth_requires_role_and_ip(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIDESK_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "view")
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "own")
    auth = AdminAuth(admin_token_env="OMNIDESK_ADMIN_TOKEN", allowed_ips=["127.0.0.1"], audit_log=tmp_path / "admin.jsonl")

    viewer = Headers({"authorization": "Bearer view", "x-omnidesk-admin-role": "owner"})
    assert not auth.verify_headers(viewer, "127.0.0.1", required_role="owner").ok

    owner = Headers({"authorization": "Bearer own", "x-omnidesk-admin-role": "viewer"})
    assert auth.verify_headers(owner, "127.0.0.1", required_role="owner").ok
    assert not auth.verify_headers(owner, "10.0.0.9", required_role="viewer").ok
    assert (tmp_path / "admin.jsonl").exists()
