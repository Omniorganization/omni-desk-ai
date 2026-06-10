from __future__ import annotations

from collections import Counter
from typing import Any


class FailureAnalyzer:
    """Classify failures into reusable learning categories."""

    CATEGORIES = {
        "captcha_required": ["captcha", "验证", "verify you are human", "robot check"],
        "login_required": ["login", "log in", "signin", "sign in", "登录"],
        "permission_denied": ["permission", "denied", "ApprovalRequired", "not approved", "forbidden"],
        "network_timeout": ["timeout", "timed out", "connection reset", "network"],
        "selector_changed": ["element not found", "selector", "not clickable", "no such element"],
        "model_misunderstanding": ["invalid plan", "unsupported action", "unknown tool", "json schema"],
        "tool_error": ["ToolError", "failed", "exception", "traceback"],
        "missing_dependency": ["No module named", "ImportError", "ModuleNotFoundError", "command not found"],
        "security_violation": ["blocked", "denylist", "allowlist", "security", "signature mismatch"],
    }

    def classify(self, task_trace: dict[str, Any] | None = None, error: str | None = None) -> str:
        text_parts = [error or ""]
        if task_trace:
            text_parts.append(str(task_trace.get("status", "")))
            for r in task_trace.get("results", []) or []:
                text_parts.append(str(r.get("error", "")))
                text_parts.append(str(r.get("summary", "")))
        haystack = "\n".join(text_parts).lower()
        for category, needles in self.CATEGORIES.items():
            if any(n.lower() in haystack for n in needles):
                return category
        return "unknown"

    def summarize_repeated_failures(self, experiences: list[dict[str, Any]], threshold: int = 3) -> list[dict[str, Any]]:
        counter = Counter(
            e.get("failure_reason") or "unknown"
            for e in experiences
            if not e.get("success")
        )
        return [
            {
                "failure_reason": reason,
                "count": count,
                "priority": "high" if count >= threshold else "medium",
                "recommended_upgrade": self.recommended_upgrade(reason),
            }
            for reason, count in counter.most_common()
            if count >= 1
        ]

    def recommended_upgrade(self, failure_reason: str) -> str:
        mapping = {
            "captcha_required": "Add explicit human handoff skill for CAPTCHA/login verification.",
            "login_required": "Add login-state detection before attempting UI automation.",
            "permission_denied": "Improve approval request wording and remote approval resume flow.",
            "network_timeout": "Add retry/backoff and network health checks.",
            "selector_changed": "Add visual fallback locator and selector fallback strategy.",
            "model_misunderstanding": "Improve ToolSpec JSON Schema and planner examples.",
            "tool_error": "Add regression tests around failing tool/action pair.",
            "missing_dependency": "Add dependency check to doctor and CI.",
            "security_violation": "Tighten policy docs and avoid unsafe actions in planner.",
            "unknown": "Add richer trace capture and manual failure labeling.",
        }
        return mapping.get(failure_reason, mapping["unknown"])
