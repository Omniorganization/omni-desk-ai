from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Optional, Union

from omnidesk_agent.self_learning.observability.schema import LearningEvent


class LearningAuditLog:
    """Append-only JSONL audit log for learning quality evidence."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, event: Union[LearningEvent, dict]) -> dict:
        if isinstance(event, dict):
            event = LearningEvent.from_dict(event)
        record = event.to_dict()
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return record

    def read(self, *, since_ts: Optional[float] = None, limit: Optional[int] = None) -> list[LearningEvent]:
        if not self.path.exists():
            return []
        events: list[LearningEvent] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = LearningEvent.from_dict(json.loads(line))
                except Exception:
                    continue
                if since_ts is not None and event.created_at < since_ts:
                    continue
                events.append(event)
        if limit is not None and limit >= 0:
            return events[-limit:]
        return events

    def read_days(self, days: int) -> list[LearningEvent]:
        since = time.time() - max(days, 0) * 86400
        return self.read(since_ts=since)
