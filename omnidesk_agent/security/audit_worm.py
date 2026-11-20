from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    # --- chain anchors ---------------------------------------------------------------------
    # A whole-file digest (create/verify above) stops matching as soon as one line
    # is appended, so it can only attest a frozen file. An anchor instead records a
    # verified chain position (sequence number, entry hash, byte offset), signed
    # with the audit key. A later verification must still find that exact entry, so
    # cutting entries off the end of a live log is caught too. Mirror anchor files
    # to immutable storage: whoever can delete them can hide a truncation.

    ANCHOR_FORMAT = "omnidesk-audit-anchor-v1"

    def create_anchor(self, audit_log: Path, report: Any, raw_key: bytes) -> Path:
        last = report.last
        if not report.ok or last is None:
            raise ValueError("only a verified audit chain can be anchored")
        record = {
            "format": self.ANCHOR_FORMAT,
            "audit_log": str(Path(audit_log).expanduser().resolve()),
            "seq": last.seq,
            "hash": last.hash,
            "offset": last.offset,
            "entries": report.entries,
            "created_at": time.time(),
        }
        record["signature"] = self._anchor_signature(record, raw_key)
        out = self.checkpoint_dir / f"audit-anchor-{int(record['created_at'] * 1000)}-{last.hash[:12]}.json"
        out.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return out

    def latest_anchor(self, audit_log: Path, raw_key: bytes) -> dict[str, Any] | None:
        """Newest anchor for this log. A forged or unreadable anchor raises ValueError."""
        target = str(Path(audit_log).expanduser().resolve())
        newest: dict[str, Any] | None = None
        for path in sorted(self.checkpoint_dir.glob("audit-anchor-*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"unreadable audit anchor: {path.name}") from exc
            if not isinstance(record, dict) or record.get("audit_log") != target:
                continue
            signature = record.get("signature")
            if not isinstance(signature, str) or not hmac.compare_digest(signature, self._anchor_signature(record, raw_key)):
                raise ValueError(f"audit anchor signature does not verify: {path.name}")
            if newest is None or float(record.get("created_at", 0)) > float(newest.get("created_at", 0)):
                newest = record
        return newest

    def verify_and_anchor(self, audit_log: Path, raw_key: bytes) -> dict[str, Any]:
        """Verify the whole chain, check it still holds the newest anchor, then anchor its tail."""
        from omnidesk_agent.security.audit_chain import verify_audit_log

        try:
            anchor = self.latest_anchor(audit_log, raw_key)
        except ValueError as exc:
            return {"path": str(audit_log), "ok": False, "error": str(exc)}
        report = verify_audit_log(audit_log, raw_key, want_points={int(anchor["seq"])} if anchor else None)
        result = report.summary()
        result["anchor_checked"] = anchor is not None
        if report.ok and anchor is not None:
            point = report.points.get(int(anchor["seq"]))
            if point is None or point.hash != anchor["hash"] or point.offset != anchor["offset"]:
                result["ok"] = False
                result["error"] = "the log no longer holds the anchored entry (entries removed or rewritten since the last check)"
        if result["ok"]:
            result["anchor_written"] = self.create_anchor(audit_log, report, raw_key).name
        return result

    @staticmethod
    def _anchor_signature(record: dict[str, Any], raw_key: bytes) -> str:
        body = {k: v for k, v in record.items() if k != "signature"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(raw_key, b"omnidesk/audit-anchor/v1\n" + canonical, hashlib.sha256).hexdigest()

    def verify(self, checkpoint_file: Path, audit_log: Path) -> bool:
        record = json.loads(Path(checkpoint_file).read_text(encoding="utf-8"))
        actual = hashlib.sha256(Path(audit_log).expanduser().read_bytes()).hexdigest()
        if actual != record.get("audit_log_sha256"):
            return False
        payload = {k: record[k] for k in ("audit_log", "audit_log_sha256", "created_at", "label") if k in record}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        checkpoint_hash = record.get("checkpoint_hash")
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not isinstance(checkpoint_hash, str) or not hmac.compare_digest(expected_hash.encode("utf-8"), checkpoint_hash.encode("utf-8")):
            return False
        key = os.getenv(self.hmac_key_env, "")
        if not key:
            return True
        # With a key configured, an unsigned (or stripped-signature) checkpoint
        # must fail closed instead of downgrading to hash-only verification.
        signature = record.get("signature")
        if not isinstance(signature, str) or not signature:
            return False
        expected = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
