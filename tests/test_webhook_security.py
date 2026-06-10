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
