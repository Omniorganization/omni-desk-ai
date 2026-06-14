from __future__ import annotations

from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.memory.governed_writer import GovernedMemoryWriter


def test_governed_memory_writer_redacts_and_namespaces():
    writer = GovernedMemoryWriter()
    result = writer.prepare({"text": "email a@example.com"}, channel="gmail", actor="u1")
    assert result.ok
    assert result.namespace == "gmail:u1"
    assert "a@example.com" not in result.payload["text"]
    assert result.payload["expires_at"] > 0


def test_experience_store_blocks_credential_like_structured_memory(tmp_path):
    with ExperienceStore(tmp_path / "m.sqlite3") as store:
        rowid = store.add_experience({"task_type": "x", "goal": "oauth token secret", "success": True, "privacy_level": "policy"}, channel="gmail", actor="u")
        assert rowid == -1
        audit_rows = store.conn.execute("SELECT * FROM memory_governance_audit").fetchall()
        assert audit_rows
