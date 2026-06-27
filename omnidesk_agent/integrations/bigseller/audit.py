from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Iterable

from omnidesk_agent.integrations.bigseller.schemas import BigSellerAuditEvent


class BigSellerAuditLogger:
    def __init__(self, audit_log_path: Path | None = None):
        self.audit_log_path = audit_log_path.expanduser() if audit_log_path else None
        self._events: list[BigSellerAuditEvent] = []
        self._lock = Lock()

    def append(self, event: BigSellerAuditEvent) -> None:
        with self._lock:
            self._events.append(event)
            if self.audit_log_path is not None:
                self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            event.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )

    def recent(self, *, limit: int = 50) -> list[BigSellerAuditEvent]:
        with self._lock:
            return list(self._events[-max(0, limit) :])

    def extend(self, events: Iterable[BigSellerAuditEvent]) -> None:
        for event in events:
            self.append(event)
