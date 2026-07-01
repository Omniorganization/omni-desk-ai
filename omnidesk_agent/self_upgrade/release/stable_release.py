from __future__ import annotations
import json
import time
from pathlib import Path
class StableReleaseManager:
    def __init__(self, release_log: Path):
        self.release_log = release_log.expanduser()
        self.release_log.parent.mkdir(parents=True, exist_ok=True)

    def promote(self, target: str, version: str, evidence: dict) -> dict:
        record={"target":target,"version":version,"channel":"stable","evidence":evidence,"promoted_at":time.time()}
        with self.release_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False)+"\n")
        return record
