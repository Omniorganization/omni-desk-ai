"""Stopping agent work: a per-run cancel and a runtime-wide emergency stop.

Cancel sets the run's row to `cancelled`; the loop reads the row before every
step and turn and ends the run there (a tool call already running is not
interrupted), and a run waiting for approval can no longer be resumed.

The emergency stop is a file next to the audit log, outside the workspace, so
every process on the host sees it and the agent's file tool cannot reach it.
While it is engaged, loops stop between steps, new runs end at once, and the
tool registry refuses every call. An unreadable stop file counts as engaged.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class EmergencyStopEngaged(PermissionError):
    """Refused because the emergency stop is engaged."""


@dataclass(frozen=True)
class StopState:
    engaged: bool
    reason: str = ""
    actor: str = ""
    engaged_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason, "actor": self.actor, "engaged_at": self.engaged_at}


class EmergencyStop:
    FILE_NAME = "emergency_stop.json"

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def status(self) -> StopState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return StopState(False)
        except (OSError, ValueError):
            return StopState(True, reason="emergency stop file is unreadable; treated as engaged")
        if not isinstance(data, dict):
            return StopState(True, reason="emergency stop file is malformed; treated as engaged")
        return StopState(True, reason=str(data.get("reason") or ""), actor=str(data.get("actor") or ""), engaged_at=data.get("engaged_at"))

    def check(self) -> None:
        state = self.status()
        if state.engaged:
            by = f" by {state.actor}" if state.actor else ""
            raise EmergencyStopEngaged(f"emergency stop engaged{by}: {state.reason or 'no reason given'}")

    def engage(self, *, reason: str, actor: str) -> StopState:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = StopState(True, reason=str(reason)[:500], actor=str(actor)[:200], engaged_at=time.time())
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        return state

    def release(self) -> StopState:
        self.path.unlink(missing_ok=True)
        return self.status()
