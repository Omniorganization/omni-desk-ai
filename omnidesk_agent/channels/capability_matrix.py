from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from omnidesk_agent.channels.ecosystem import channel_catalog

ActionRisk = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class ChannelCapability:
    channel: str
    allowed_actions: tuple[str, ...]
    denied_actions: tuple[str, ...]
    required_controls: tuple[str, ...]
    default_risk: ActionRisk
    attachments_allowed: bool = False
    links_allowed: bool = False
    ui_bridge_allowed: bool = False
    owner_approval_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_HIGH_RISK_OWNER_ACTIONS = (
    "send_external_message",
    "send_attachment",
    "open_link",
    "ui_bridge_automation",
    "write_memory",
    "run_tool",
)


def build_channel_capability_matrix(*, include_reference: bool = True) -> dict[str, ChannelCapability]:
    matrix: dict[str, ChannelCapability] = {}
    for entry in channel_catalog(include_reference=include_reference):
        ui_bridge_allowed = "ui_bridge" in entry.surfaces or entry.ui_bridge_app is not None
        allowed = ["receive_message", "request_approval", "create_task"]
        denied = ["read_secret", "change_policy", "self_modify", "disable_audit", "bypass_approval"]
        if entry.outbound:
            allowed.append("send_external_message")
        if ui_bridge_allowed:
            allowed.append("ui_bridge_automation")
        if entry.risk in {"high", "critical"}:
            owner_actions = _HIGH_RISK_OWNER_ACTIONS
        else:
            owner_actions = ("send_attachment", "ui_bridge_automation")
        matrix[entry.name] = ChannelCapability(
            channel=entry.name,
            allowed_actions=tuple(allowed),
            denied_actions=tuple(denied),
            required_controls=entry.required_controls,
            default_risk=entry.risk,
            attachments_allowed=entry.status == "native_adapter",
            links_allowed=True,
            ui_bridge_allowed=ui_bridge_allowed,
            owner_approval_actions=owner_actions,
        )
    return matrix


def channel_capability_matrix(*, include_reference: bool = True) -> list[dict[str, Any]]:
    return [item.to_dict() for item in build_channel_capability_matrix(include_reference=include_reference).values()]


def evaluate_channel_action(channel: str, action: str, *, risk: ActionRisk | None = None) -> dict[str, Any]:
    capability = build_channel_capability_matrix().get(channel)
    if capability is None:
        return {
            "allowed": False,
            "reason": "unknown_channel",
            "requires_owner_approval": True,
            "required_controls": ("operator_pairing", "audit_log"),
        }
    if action in capability.denied_actions:
        return {
            "allowed": False,
            "reason": "denied_by_channel_capability_matrix",
            "requires_owner_approval": True,
            "required_controls": capability.required_controls,
        }
    effective_risk = risk or capability.default_risk
    requires_owner = action in capability.owner_approval_actions or effective_risk in {"high", "critical"}
    return {
        "allowed": action in capability.allowed_actions,
        "reason": "allowed" if action in capability.allowed_actions else "not_declared_for_channel",
        "requires_owner_approval": requires_owner,
        "required_controls": capability.required_controls,
        "risk": effective_risk,
    }
