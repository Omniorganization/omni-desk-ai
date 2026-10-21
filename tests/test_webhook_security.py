from __future__ import annotations
import time
import pytest
from omnidesk_agent.security.webhook_security import WebhookSecurity, WebhookSecurityConfig


def test_webhook_replay_blocked(tmp_path):
    guard = WebhookSecurity(tmp_path / "webhooks.sqlite3", WebhookSecurityConfig(rate_limit_max_requests=10))
    body = b'{"id":"1"}'
    assert guard.guard(channel="telegram", body=body, source_key="u1", message_id="m1")["ok"]
    with pytest.raises(PermissionError):
        guard.guard(channel="telegram", body=body, source_key="u1", message_id="m1")


def test_webhook_timestamp_window(tmp_path):
    guard = WebhookSecurity(tmp_path / "webhooks.sqlite3", WebhookSecurityConfig(replay_ttl_seconds=1))
    with pytest.raises(PermissionError):
        guard.guard(channel="x", body=b"x", source_key="u", timestamp=time.time() - 99)


def test_seen_digests_outlive_the_timestamp_window(tmp_path, monkeypatch):
    guard = WebhookSecurity(tmp_path / "webhooks.sqlite3", WebhookSecurityConfig(replay_ttl_seconds=300))
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    assert guard.guard(channel="dingtalk", body=b"signed", source_key="u", message_id="evt-1")["ok"]
    # A captured request without a timestamp replayed an hour later is still rejected.
    monkeypatch.setattr(time, "time", lambda: now + 3600)
    with pytest.raises(PermissionError, match="duplicate"):
        guard.guard(channel="dingtalk", body=b"signed", source_key="u", message_id="evt-1")
