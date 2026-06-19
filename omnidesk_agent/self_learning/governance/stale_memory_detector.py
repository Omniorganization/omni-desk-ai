from __future__ import annotations

import time
from typing import Any


class StaleMemoryDetector:
    def __init__(self, max_age_days: int = 90):
        self.max_age_days = max_age_days

    def is_stale(self, experience: dict[str, Any]) -> bool:
        expires_at = experience.get("expires_at")
        if expires_at is not None and float(expires_at) <= time.time():
            return True
        updated_at = float(experience.get("updated_at") or experience.get("created_at") or time.time())
        return (time.time() - updated_at) > self.max_age_days * 86400
