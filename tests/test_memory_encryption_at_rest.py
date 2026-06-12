from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import MemoryPrivacyConfig
from omnidesk_agent.memory.experience import ExperienceStore


def test_memory_encrypt_at_rest_hides_plaintext_in_sqlite_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_MEMORY_ENCRYPTION_KEY", "unit-test-memory-key")
    db = tmp_path / "memory.sqlite3"
    cfg = MemoryPrivacyConfig(encrypt_at_rest=True, encryption_key_env="OMNIDESK_MEMORY_ENCRYPTION_KEY")

    secret = "customer private selector changed"
    with ExperienceStore(db, cfg) as store:
        store.add(secret, plan="click login", outcome="campaign loaded", tags=["sensitive"])
        store.add_experience({
            "task_type": "browser",
            "goal": secret,
            "success": False,
            "failure_reason": "selector_changed",
            "recommended_next_action": "update parser",
            "raw_trace": {"private": secret},
            "tags": ["tiktok"],
        })
        assert store.search("selector changed", limit=1)[0]["task"] == secret
        assert store.search_similar("selector", limit=1)[0]["goal"] == secret

    raw = db.read_bytes()
    assert secret.encode("utf-8") not in raw
    assert b"click login" not in raw
    assert b"campaign loaded" not in raw
