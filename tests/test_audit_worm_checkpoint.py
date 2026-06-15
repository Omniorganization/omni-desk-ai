from __future__ import annotations

from omnidesk_agent.security.audit_worm import WormAuditCheckpoint


def test_worm_audit_checkpoint_signs_verifies_and_detects_tampering(tmp_path, monkeypatch):
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text('{"event":"admin"}\n', encoding="utf-8")
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", "secret-key")

    writer = WormAuditCheckpoint(checkpoint_dir)
    checkpoint = writer.create(audit_log, label="daily")
    checkpoint_file = next(checkpoint_dir.glob("audit-checkpoint-*.json"))

    assert checkpoint.signature
    assert writer.verify(checkpoint_file, audit_log) is True

    audit_log.write_text('{"event":"tampered"}\n', encoding="utf-8")
    assert writer.verify(checkpoint_file, audit_log) is False


def test_worm_audit_checkpoint_allows_unsigned_local_verification(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", raising=False)
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text('{"event":"local"}\n', encoding="utf-8")
    writer = WormAuditCheckpoint(tmp_path / "checkpoints")

    checkpoint = writer.create(audit_log)
    checkpoint_file = next((tmp_path / "checkpoints").glob("audit-checkpoint-*.json"))

    assert checkpoint.signature is None
    assert writer.verify(checkpoint_file, audit_log) is True
