from __future__ import annotations

from collections import deque
from threading import Lock
import time
from typing import Callable


class BigSellerRateLimiter:
    """Small sliding-window limiter for outbound BigSeller API calls."""

    def __init__(
        self,
        *,
        per_minute: int = 60,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.per_minute = max(1, int(per_minute))
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self._calls: deque[float] = deque()
        self._lock = Lock()

    def delay_seconds(self) -> float:
        now = self.clock()
        with self._lock:
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) < self.per_minute:
                self._calls.append(now)
                return 0.0
            return max(0.0, 60 - (now - self._calls[0]))

    def wait(self) -> float:
        delay = self.delay_seconds()
        if delay > 0:
            self.sleeper(delay)
            with self._lock:
                self._calls.append(self.clock())
        return delay
