from __future__ import annotations

from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter


def test_privacy_filter_redacts_common_secrets():
    redactor = MemoryPrivacyFilter()
    text = redactor.redact_text("email a@example.com token=abc123 phone +6012 345 6789")
    assert "a@example.com" not in text
    assert "abc123" not in text
    assert "+6012" not in text


def test_experience_store_redacts_before_persist(tmp_path):
    store = ExperienceStore(tmp_path / "m.sqlite3")
    store.add("reply to a@example.com", plan="token=abc123", outcome="ok")
    rows = store.search("reply", limit=1)
    assert rows
    assert "a@example.com" not in rows[0]["task"]
    assert "abc123" not in rows[0]["plan"]
