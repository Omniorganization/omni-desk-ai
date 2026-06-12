from __future__ import annotations

import time

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.security.webhook_security import WebhookSecurity, WebhookSecurityConfig


def test_permission_manager_modes_session_and_auto(tmp_path):
    cfg = PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.jsonl")
    pm = PermissionManager(cfg)
    proposal = {"tool": "safe", "action": "read", "risk": "low", "source": "local-cli", "actor": "u"}
    assert pm.verify(proposal).allowed

    cfg = PermissionConfig(default_mode="dry_run", audit_log=tmp_path / "audit2.jsonl")
    pm = PermissionManager(cfg)
    assert pm.verify({**proposal, "risk": "high"}).mode == "dry_run"

    cfg = PermissionConfig(approval_mode="auto_policy", audit_log=tmp_path / "audit3.jsonl")
    pm = PermissionManager(cfg)
    assert pm.verify({**proposal, "risk": "medium", "tool": "safe"}).allowed

    key = pm.allow_approved_proposal({**proposal, "risk": "high", "scope_hash": "abc"})
    assert key in pm.session_allows
    assert pm.verify({**proposal, "risk": "high", "scope_hash": "abc"}).allowed


def test_webhook_security_hmac_replay_timestamp_and_rate_limit(tmp_path):
    sec = WebhookSecurity(tmp_path / "webhook.sqlite3", WebhookSecurityConfig(replay_ttl_seconds=10, rate_limit_window_seconds=60, rate_limit_max_requests=1))
    body = b'{"ok":true}'
    sig = __import__("hmac").new(b"secret", body, __import__("hashlib").sha256).hexdigest()
    sec.verify_hmac_sha256_hex(body, "secret", "sha256=" + sig, prefix="sha256=")
    with pytest.raises(PermissionError):
        sec.verify_hmac_sha256_hex(body, "secret", "bad")

    first = sec.guard(channel="telegram", body=body, source_key="user", message_id="m1", timestamp=time.time())
    assert first["ok"]
    with pytest.raises(PermissionError, match="rate limit"):
        sec.guard(channel="telegram", body=b"2", source_key="user", message_id="m2", timestamp=time.time())

    sec2 = WebhookSecurity(tmp_path / "webhook2.sqlite3")
    sec2.guard(channel="telegram", body=body, source_key="user", message_id="m1", timestamp=time.time())
    with pytest.raises(PermissionError, match="duplicate"):
        sec2.guard(channel="telegram", body=body, source_key="user", message_id="m1", timestamp=time.time())
    with pytest.raises(PermissionError, match="timestamp"):
        sec2.guard(channel="telegram", body=b"3", source_key="user2", timestamp=time.time() - 10000)


def test_permission_manager_no_tty_and_interactive_paths(monkeypatch, tmp_path):
    proposal = {"tool": "danger", "action": "x", "risk": "high", "source": "remote", "actor": "u"}

    pm = PermissionManager(PermissionConfig(default_mode="deny", audit_log=tmp_path / "deny.jsonl"))
    with pytest.raises(PermissionError, match="Denied by default"):
        pm.verify(proposal)

    pm = PermissionManager(PermissionConfig(no_tty_mode="dry_run", audit_log=tmp_path / "notty.jsonl"))
    with monkeypatch.context() as m:
        m.setattr("sys.stdin.isatty", lambda: False)
        assert pm.verify(proposal).mode == "dry_run"

    pm = PermissionManager(PermissionConfig(audit_log=tmp_path / "interactive.jsonl"))
    with monkeypatch.context() as m:
        m.setattr("sys.stdin.isatty", lambda: True)
        m.setattr("builtins.input", lambda prompt: "s")
        assert pm.verify(proposal).mode == "allow"
        assert pm.verify(proposal).mode == "allow"

    pm = PermissionManager(PermissionConfig(audit_log=tmp_path / "interactive_dry.jsonl"))
    with monkeypatch.context() as m:
        m.setattr("sys.stdin.isatty", lambda: True)
        m.setattr("builtins.input", lambda prompt: "d")
        assert pm.verify(proposal).mode == "dry_run"

    pm = PermissionManager(PermissionConfig(audit_log=tmp_path / "interactive_deny.jsonl"))
    with monkeypatch.context() as m:
        m.setattr("sys.stdin.isatty", lambda: True)
        m.setattr("builtins.input", lambda prompt: "n")
        with pytest.raises(PermissionError, match="Denied by user"):
            pm.verify(proposal)


def test_admin_auth_ambiguous_and_legacy_paths(monkeypatch, tmp_path):
    from omnidesk_agent.security.admin_auth import AdminAuth

    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "same")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "same")
    auth = AdminAuth(audit_log=tmp_path / "admin.jsonl")
    denied = auth.verify_headers({"authorization": "Bearer same"}, client_host="127.0.0.1")
    assert not denied.ok
    assert "ambiguous" in denied.reason

    monkeypatch.delenv("OMNIDESK_VIEWER_TOKEN")
    monkeypatch.delenv("OMNIDESK_OPERATOR_TOKEN")
    monkeypatch.setenv("OMNIDESK_GATEWAY_SECRET", "legacy")
    auth = AdminAuth(legacy_secret_env="OMNIDESK_GATEWAY_SECRET", audit_log=tmp_path / "legacy.jsonl")
    assert auth.verify_headers({"x-omnidesk-gateway-secret": "legacy"}, client_host="127.0.0.1", required_role="owner").ok
    assert not auth.verify_headers({}, client_host="127.0.0.1").ok
