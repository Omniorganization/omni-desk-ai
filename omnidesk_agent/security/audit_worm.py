from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditCheckpoint:
    audit_log_sha256: str
    checkpoint_hash: str
    signature: str | None
    created_at: float


class WormAuditCheckpoint:
    """Append-only audit checkpoint writer/verifier.

    It does not pretend local files are a real WORM appliance. The checkpoint is
    structured so production can mirror it to immutable object storage with
    retention lock enabled, while tests and small deployments still get a local
    verifier.
    """

    def __init__(self, checkpoint_dir: Path, *, hmac_key_env: str = "OMNIDESK_AUDIT_CHECKPOINT_HMAC_KEY"):
        self.checkpoint_dir = Path(checkpoint_dir).expanduser()
        self.hmac_key_env = hmac_key_env
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def create(self, audit_log: Path, *, label: str | None = None) -> AuditCheckpoint:
        data = Path(audit_log).expanduser().read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        created_at = time.time()
        payload = {"audit_log": str(audit_log), "audit_log_sha256": digest, "created_at": created_at, "label": label or "daily"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checkpoint_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        key = os.getenv(self.hmac_key_env, "")
        signature = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest() if key else None
        record = {**payload, "checkpoint_hash": checkpoint_hash, "signature": signature, "signature_algorithm": "hmac-sha256" if signature else "unsigned-local"}
        out = self.checkpoint_dir / f"audit-checkpoint-{int(created_at)}-{checkpoint_hash[:12]}.json"
        out.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return AuditCheckpoint(digest, checkpoint_hash, signature, created_at)

    def verify(self, checkpoint_file: Path, audit_log: Path) -> bool:
        record = json.loads(Path(checkpoint_file).read_text(encoding="utf-8"))
        actual = hashlib.sha256(Path(audit_log).expanduser().read_bytes()).hexdigest()
        if actual != record.get("audit_log_sha256"):
            return False
        key = os.getenv(self.hmac_key_env, "")
        signature = record.get("signature")
        if signature and key:
            payload = {k: record[k] for k in ("audit_log", "audit_log_sha256", "created_at", "label") if k in record}
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            expected = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        return bool(record.get("checkpoint_hash"))
