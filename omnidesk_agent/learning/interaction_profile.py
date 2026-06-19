from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any, Optional

from omnidesk_agent.channels.ecosystem import recommend_surface


@dataclass(frozen=True)
class InteractionSignal:
    source_channel: str
    actor: str
    target_channel: Optional[str]
    display_name: Optional[str]
    surface: str
    ui_bridge_app: Optional[str]
    status: str
    risk: str
    required_controls: tuple[str, ...]
    source_reference: str
    confidence: float
    learned_profile_used: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_interaction_signal(
    text: str,
    *,
    source_channel: str = "unknown",
    actor: str = "unknown",
    learned_profile: Optional[dict[str, Any]] = None,
) -> InteractionSignal:
    """Infer product-interaction preference without granting new permissions."""

    learned_surface = None
    learned_profile_used = False
    if learned_profile:
        task_count = int(learned_profile.get("task_count", 0) or 0)
        success_count = int(learned_profile.get("success_count", 0) or 0)
        confidence = float(learned_profile.get("confidence", 0.0) or 0.0)
        if task_count >= 2 and success_count > 0 and confidence >= 0.4:
            learned_surface = str(learned_profile.get("preferred_surface") or "") or None
            learned_profile_used = learned_surface is not None

    recommendation = recommend_surface(text, learned_surface=learned_surface)
    confidence = 0.65 if recommendation["target_channel"] else 0.35
    if learned_profile_used:
        confidence = min(0.95, confidence + 0.2)

    return InteractionSignal(
        source_channel=source_channel,
        actor=actor,
        target_channel=recommendation["target_channel"],
        display_name=recommendation["display_name"],
        surface=recommendation["surface"],
        ui_bridge_app=recommendation["ui_bridge_app"],
        status=recommendation["status"],
        risk=recommendation["risk"],
        required_controls=tuple(recommendation["required_controls"]),
        source_reference=recommendation["source_reference"],
        confidence=round(confidence, 4),
        learned_profile_used=learned_profile_used,
    )


def profile_from_row(row: dict[str, Any]) -> dict[str, Any]:
    task_count = int(row.get("task_count", 0) or 0)
    success_count = int(row.get("success_count", 0) or 0)
    manual_count = int(row.get("manual_intervention_count", 0) or 0)
    safety_count = int(row.get("safety_violation_count", 0) or 0)
    if task_count <= 0:
        confidence = 0.0
    else:
        success_rate = success_count / task_count
        friction_penalty = min(0.4, manual_count / task_count * 0.2 + safety_count / task_count * 0.4)
        confidence = max(0.0, min(1.0, success_rate - friction_penalty))
    out = dict(row)
    out["confidence"] = round(confidence, 4)
    return out


def empty_profile(channel: str, actor: str) -> dict[str, Any]:
    now = time.time()
    return {
        "namespace": f"{channel}:{actor}",
        "channel": channel,
        "actor": actor,
        "task_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "manual_intervention_count": 0,
        "safety_violation_count": 0,
        "preferred_surface": "local_gateway",
        "preferred_channel": "unknown",
        "preferred_app": None,
        "last_task": "",
        "last_status": "unknown",
        "updated_at": now,
        "confidence": 0.0,
    }
