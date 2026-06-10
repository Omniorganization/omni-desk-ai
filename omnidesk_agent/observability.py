from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional


def new_request_id() -> str:
    return str(uuid.uuid4())


class JsonEventLogger:
    def __init__(self, name: str = "omnidesk"):
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def event(self, event: str, **fields: Any) -> None:
        payload = {"event": event, "ts": time.time(), **fields}
        self.logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


@dataclass
class MetricsRegistry:
    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.gauges[key] = value

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self.counters.items()):
                lines.append(f"{key} {value}")
            for key, value in sorted(self.gauges.items()):
                lines.append(f"{key} {value}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> str:
        if not labels:
            return name
        label_text = ",".join(f'{k}="{str(v).replace(chr(34), chr(92)+chr(34))}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_text}}}"


def public_runtime_status(version: str) -> dict[str, Any]:
    return {"ok": True, "version": version}


def redact_runtime_status(status: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(status)
    for key in ("workspace", "audit_log"):
        if key in redacted:
            redacted[key] = "<redacted>"
    return redacted
