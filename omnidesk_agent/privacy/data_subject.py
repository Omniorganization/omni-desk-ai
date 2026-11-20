"""Data-subject access (export) and erasure across the gateway's stores.

A store takes part by implementing two methods:

    export_subject(actor: str) -> list[dict]   # JSON-safe records about the actor
    erase_subject(actor: str) -> int           # records deleted or anonymised

Every store the runtime holds is listed. One without the methods is reported
as ``unsupported`` instead of being skipped silently, so an export or erase
result always says whether it is complete. Some records are kept by design and
reported as ``retained`` with the reason, such as the tamper-evident security
audit logs: rewriting them would destroy the evidence they exist for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# Payload fields of an audit entry that name the person it concerns.
_SUBJECT_FIELDS = ("actor", "created_by", "proposer", "requester", "requested_by", "approved_by", "approver", "sender_id", "revoked_by")


def supports_subject_requests(store: Any) -> bool:
    return callable(getattr(store, "export_subject", None)) and callable(getattr(store, "erase_subject", None))


class AuditLogSubjectSource:
    """Read-only view of an audit log's entries about one actor (export, never erase)."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def export_subject(self, actor: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
                if any(str(payload.get(field) or "") == actor for field in _SUBJECT_FIELDS):
                    records.append({"event": entry.get("event"), "ts": entry.get("ts"), "payload": payload})
        return records


# Erased before every other source (see DataSubjectService.erase).
ERASE_FIRST = ("runs", "jobs")


class DataSubjectService:
    def __init__(
        self,
        sources: Mapping[str, Any],
        *,
        retained: Optional[Mapping[str, tuple[Any, str]]] = None,
        audit: Optional[Callable[[str, dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        """``sources``: name -> store; ``retained``: name -> (read-only source, reason kept)."""
        self.sources = {name: store for name, store in sources.items() if store is not None}
        self.retained = dict(retained or {})
        self.audit = audit
        self.clock = clock

    def export(self, actor: str, *, requested_by: str) -> dict[str, Any]:
        actor = _require_actor(actor)
        data: dict[str, list[dict[str, Any]]] = {}
        unsupported: list[str] = []
        for name, store in sorted(self.sources.items()):
            if supports_subject_requests(store):
                data[name] = list(store.export_subject(actor))
            else:
                unsupported.append(name)
        retained = {}
        for name, (source, reason) in sorted(self.retained.items()):
            data[name] = list(source.export_subject(actor)) if source is not None else []
            retained[name] = reason
        result = {
            "actor": actor,
            "generated_at": self.clock(),
            "complete": not unsupported,
            "unsupported": unsupported,
            "retained": retained,
            "data": data,
        }
        self._audit("privacy.subject_exported", {"subject": actor, "requested_by": requested_by, "counts": {k: len(v) for k, v in data.items()}, "unsupported": unsupported})
        return result

    def erase(self, actor: str, *, requested_by: str, approval_id: str) -> dict[str, Any]:
        actor = _require_actor(actor)
        erased: dict[str, int] = {}
        unsupported: list[str] = []
        # Stores that start work go first: once the subject's runs and jobs are
        # tombstoned, a run finishing meanwhile cannot write new memory about them.
        first = [name for name in ERASE_FIRST if name in self.sources]
        order = first + sorted(name for name in self.sources if name not in first)
        for name, store in ((name, self.sources[name]) for name in order):
            if supports_subject_requests(store):
                erased[name] = int(store.erase_subject(actor))
            else:
                unsupported.append(name)
        result = {
            "actor": actor,
            "erased_at": self.clock(),
            "complete": not unsupported,
            "erased": erased,
            "unsupported": unsupported,
            "retained": {name: reason for name, (_source, reason) in sorted(self.retained.items())},
        }
        self._audit("privacy.subject_erased", {"subject": actor, "requested_by": requested_by, "approval_id": approval_id, "erased": erased, "unsupported": unsupported})
        return result

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit(event, payload)


def _require_actor(actor: str) -> str:
    actor = str(actor or "").strip()
    if not actor or len(actor) > 256:
        raise ValueError("a data subject actor id is required")
    return actor
