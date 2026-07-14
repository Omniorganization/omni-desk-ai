from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from omnidesk_agent.appsync.offline_sync_apply import ApplyingAppSyncStore


class CorruptAppSyncState(RuntimeError):
    """Raised after an unreadable local AppSync state file is quarantined."""


class StrictJsonAppSyncStore(ApplyingAppSyncStore):
    """Development-only JSON store that fails closed on state corruption.

    The legacy loader intentionally ignored malformed state. That behavior can
    silently replace a damaged store with an empty in-memory state on the next
    write. This subclass quarantines the unreadable file and aborts startup so an
    operator must explicitly restore or repair it.
    """

    def _load(self) -> None:
        path = Path(self.path)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("AppSync JSON root must be an object")
        except Exception as exc:
            quarantine = path.with_name(
                f"{path.name}.corrupt.{int(time.time())}.{os.getpid()}"
            )
            try:
                os.replace(path, quarantine)
            except OSError as move_exc:
                raise CorruptAppSyncState(
                    f"AppSync state is corrupt and could not be quarantined: {path}"
                ) from move_exc
            raise CorruptAppSyncState(
                f"AppSync state is corrupt; quarantined at {quarantine}. "
                "Restore a verified backup before restarting."
            ) from exc
        super()._load()

    def health_details(self) -> dict[str, Any]:
        return {
            "backend": "json",
            "development_only": True,
            "path": str(self.path),
            "corruption_policy": "fail_closed_and_quarantine",
        }
