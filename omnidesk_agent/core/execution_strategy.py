from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ExecutionDecision:
    allowed: bool
    reason: str
    expected_result: str
    requires_screenshot: bool = False
    requires_llm: bool = False


class ResultOrientedExecutionStrategy:
    """Make expensive actions prove their purpose before execution."""

    def __init__(self):
        self._last_screenshot_hash: str | None = None
        self._last_screenshot_at = 0.0
        self._recent_expensive_actions: list[tuple[float, str, str, str]] = []

    def decide_tool_step(self, *, tool: str, action: str, args: dict[str, Any], goal: str) -> ExecutionDecision:
        expected = str(args.get("expected_result") or args.get("reason") or goal or "").strip()
        if not expected:
            return ExecutionDecision(False, "missing expected_result before execution", expected)

        expensive = tool in {"computer", "llm"} or action in {"screenshot", "analyze_screen", "complete"}
        if expensive and self._is_recent_duplicate(tool, action, expected):
            return ExecutionDecision(False, "duplicate expensive action skipped", expected)

        return ExecutionDecision(
            True,
            "result-oriented purpose accepted",
            expected,
            requires_screenshot=(tool == "computer"),
            requires_llm=(tool == "llm" or action in {"complete", "analyze_screen"}),
        )

    def decide_screenshot_analysis(self, image_bytes: bytes, *, min_interval_seconds: float = 1.0) -> ExecutionDecision:
        now = time.time()
        digest = hashlib.sha256(image_bytes).hexdigest()

        if digest == self._last_screenshot_hash:
            return ExecutionDecision(False, "same screenshot; skip visual analysis", "analyze only changed screen")

        if now - self._last_screenshot_at < min_interval_seconds:
            return ExecutionDecision(False, "screenshot too frequent; wait for UI change", "avoid repeated screen analysis")

        self._last_screenshot_hash = digest
        self._last_screenshot_at = now
        return ExecutionDecision(True, "new screenshot accepted for analysis", "understand changed UI state", requires_screenshot=True, requires_llm=True)

    def _is_recent_duplicate(self, tool: str, action: str, expected: str) -> bool:
        now = time.time()
        self._recent_expensive_actions = [x for x in self._recent_expensive_actions if now - x[0] < 30]
        key = (tool, action, expected)
        for _, t, a, e in self._recent_expensive_actions:
            if (t, a, e) == key:
                return True
        self._recent_expensive_actions.append((now, tool, action, expected))
        return False
