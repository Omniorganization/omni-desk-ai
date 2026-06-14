from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class DriftSignal:
    drift_type: str
    severity: str
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriftDetectionSuite:
    """Finds environment or policy drift from learning metrics and failures."""

    def detect(self, *, metrics: dict[str, Any], failure_counts: list[dict[str, Any]], experiences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        signals: list[DriftSignal] = []
        by_reason = {str(i.get("failure_reason")): int(i.get("count", 0) or 0) for i in failure_counts}
        total_failures = sum(by_reason.values()) or 1

        ui_count = sum(by_reason.get(reason, 0) for reason in ("selector_changed", "captcha_required", "login_required"))
        if ui_count / total_failures >= 0.4 and ui_count >= 2:
            signals.append(DriftSignal("ui_drift", "high", "UI/login/captcha failures dominate recent failures", {"count": ui_count}))

        api_count = sum(by_reason.get(reason, 0) for reason in ("missing_dependency", "tool_error", "network_timeout"))
        if api_count / total_failures >= 0.4 and api_count >= 2:
            signals.append(DriftSignal("api_or_tool_drift", "medium", "tool/API failures are elevated", {"count": api_count}))

        if metrics.get("tool_error_rate", 0) >= 0.25:
            signals.append(DriftSignal("tool_failure_drift", "high", "tool_error_rate exceeded 0.25", {"tool_error_rate": metrics.get("tool_error_rate")}))

        if by_reason.get("model_misunderstanding", 0) >= 2:
            signals.append(DriftSignal("model_behavior_drift", "medium", "model misunderstanding repeated", {"count": by_reason["model_misunderstanding"]}))

        preference_events = [
            item for item in experiences
            if "preference" in str(item.get("goal", "")).lower() or "preference" in str(item.get("tags", "")).lower()
        ]
        if len(preference_events) >= 2:
            signals.append(DriftSignal("user_preference_drift", "low", "user preference related memories changed recently", {"count": len(preference_events)}))

        return [signal.to_dict() for signal in signals]
