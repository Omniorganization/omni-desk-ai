from __future__ import annotations

from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter


def test_privacy_filter_redacts_common_secrets():
    redactor = MemoryPrivacyFilter()
    text = redactor.redact_text("email a@example.com token=abc123 phone +6012 345 6789")
    assert "a@example.com" not in text
    assert "abc123" not in text
    assert "+6012" not in text


def test_privacy_filter_redacts_oauth_headers_and_nested_secret_keys():
    redactor = MemoryPrivacyFilter()
    text = redactor.redact_text(
        "Authorization: Bearer sk-live access_token=tok123&client_secret=sec456 refresh_token=\"ref789\" token_count=42"
    )
    assert "sk-live" not in text
    assert "tok123" not in text
    assert "sec456" not in text
    assert "ref789" not in text
    assert "token_count=42" in text

    redacted = redactor.redact_obj({"access_token": "tok123", "nested": {"refresh_token": "ref789", "token_count": 42}})
    assert redacted["access_token"] == "[REDACTED_SECRET]"
    assert redacted["nested"]["refresh_token"] == "[REDACTED_SECRET]"
    assert redacted["nested"]["token_count"] == 42


def test_experience_store_redacts_before_persist(tmp_path):
    with ExperienceStore(tmp_path / "m.sqlite3") as store:
        store.add("reply to a@example.com", plan="token=abc123", outcome="ok")
        rows = store.search("reply", limit=1)
        assert rows
        assert "a@example.com" not in rows[0]["task"]
        assert "abc123" not in rows[0]["plan"]
