from __future__ import annotations

import hashlib
import json

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


def _signed_checkpoint(tmp_path, monkeypatch):
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text('{"event":"admin"}\n', encoding="utf-8")
    monkeypatch.setenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", "secret-key")
    writer = WormAuditCheckpoint(tmp_path / "checkpoints")
    writer.create(audit_log)
    checkpoint_file = next((tmp_path / "checkpoints").glob("audit-checkpoint-*.json"))
    return writer, checkpoint_file, audit_log, json.loads(checkpoint_file.read_text(encoding="utf-8"))


def _rewrite(checkpoint_file, record):
    checkpoint_file.write_text(json.dumps(record), encoding="utf-8")


def test_worm_audit_checkpoint_rejects_stripped_signature_when_key_configured(tmp_path, monkeypatch):
    writer, checkpoint_file, audit_log, record = _signed_checkpoint(tmp_path, monkeypatch)
    assert writer.verify(checkpoint_file, audit_log) is True
    for stripped in (None, ""):
        _rewrite(checkpoint_file, {**record, "signature": stripped, "signature_algorithm": "unsigned-local"})
        assert writer.verify(checkpoint_file, audit_log) is False
    _rewrite(checkpoint_file, {k: v for k, v in record.items() if k != "signature"})
    assert writer.verify(checkpoint_file, audit_log) is False


def test_worm_audit_checkpoint_rejects_forged_signature_and_reforged_log(tmp_path, monkeypatch):
    writer, checkpoint_file, audit_log, record = _signed_checkpoint(tmp_path, monkeypatch)
    _rewrite(checkpoint_file, {**record, "signature": "0" * 64})
    assert writer.verify(checkpoint_file, audit_log) is False
    _rewrite(checkpoint_file, {**record, "signature": 12345})
    assert writer.verify(checkpoint_file, audit_log) is False

    # Attacker rewrites the log and recomputes digest + hash but cannot re-sign.
    audit_log.write_text('{"event":"forged"}\n', encoding="utf-8")
    payload = {**{k: record[k] for k in ("audit_log", "created_at", "label")}, "audit_log_sha256": hashlib.sha256(audit_log.read_bytes()).hexdigest()}
    forged_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    _rewrite(checkpoint_file, {**record, **payload, "checkpoint_hash": forged_hash, "signature": None})
    assert writer.verify(checkpoint_file, audit_log) is False


def test_worm_audit_checkpoint_unsigned_record_fails_once_key_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", raising=False)
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text('{"event":"local"}\n', encoding="utf-8")
    writer = WormAuditCheckpoint(tmp_path / "checkpoints")
    writer.create(audit_log)
    checkpoint_file = next((tmp_path / "checkpoints").glob("audit-checkpoint-*.json"))
    assert writer.verify(checkpoint_file, audit_log) is True

    monkeypatch.setenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", "secret-key")
    assert writer.verify(checkpoint_file, audit_log) is False


def test_worm_audit_checkpoint_rejects_tampered_checkpoint_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY", raising=False)
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text('{"event":"local"}\n', encoding="utf-8")
    writer = WormAuditCheckpoint(tmp_path / "checkpoints")
    writer.create(audit_log)
    checkpoint_file = next((tmp_path / "checkpoints").glob("audit-checkpoint-*.json"))
    record = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    _rewrite(checkpoint_file, {**record, "label": "rewritten"})
    assert writer.verify(checkpoint_file, audit_log) is False
    _rewrite(checkpoint_file, {**record, "checkpoint_hash": "x"})
    assert writer.verify(checkpoint_file, audit_log) is False
