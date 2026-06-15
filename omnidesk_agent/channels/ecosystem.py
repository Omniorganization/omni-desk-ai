from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal, Optional

RiskTier = Literal["low", "medium", "high", "critical"]
SupportStatus = Literal["native_adapter", "ui_bridge", "ecosystem_reference"]
ChannelCategory = Literal["messaging", "email", "social", "desktop", "voice", "visual_workspace", "control_plane"]


@dataclass(frozen=True)
class ChannelEcosystemEntry:
    """Operator-facing channel catalog entry.

    The catalog is intentionally descriptive. It does not grant runtime access;
    OmniDesk's existing channel configs, webhook signatures, allowlists, and
    approval gates remain the enforcement layer.
    """

    name: str
    display_name: str
    category: ChannelCategory
    status: SupportStatus
    aliases: tuple[str, ...]
    inbound: bool
    outbound: bool
    surfaces: tuple[str, ...]
    required_controls: tuple[str, ...]
    risk: RiskTier = "medium"
    ui_bridge_app: Optional[str] = None
    source_reference: str = "omnidesk"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BASE_CONTROLS = (
    "webhook_signature_or_oauth",
    "sender_allowlist",
    "permission_approval_gate",
    "audit_log",
)

_GROUP_CONTROLS = _BASE_CONTROLS + ("group_or_dm_admission_policy",)


OMNIDESK_NATIVE_CHANNELS: tuple[ChannelEcosystemEntry, ...] = (
    ChannelEcosystemEntry(
        "telegram",
        "Telegram",
        "messaging",
        "native_adapter",
        ("telegram", "tg"),
        True,
        True,
        ("webhook", "outbound_queue"),
        _GROUP_CONTROLS,
        ui_bridge_app="Telegram",
    ),
    ChannelEcosystemEntry(
        "whatsapp_cloud",
        "WhatsApp Cloud",
        "messaging",
        "native_adapter",
        ("whatsapp", "whatsapp cloud", "whatsapp business"),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="WhatsApp",
    ),
    ChannelEcosystemEntry(
        "wechat_official",
        "WeChat Official",
        "messaging",
        "native_adapter",
        ("wechat", "weixin", "微信", "公众号"),
        True,
        True,
        ("webhook", "passive_reply", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        risk="high",
        ui_bridge_app="WeChat",
    ),
    ChannelEcosystemEntry(
        "meta_graph",
        "Meta Graph",
        "social",
        "native_adapter",
        ("meta", "facebook", "instagram", "messenger"),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        risk="high",
        ui_bridge_app="Instagram",
    ),
    ChannelEcosystemEntry(
        "dingtalk",
        "DingTalk",
        "messaging",
        "native_adapter",
        ("dingtalk", "钉钉"),
        True,
        True,
        ("webhook", "signed_robot", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="DingTalk",
    ),
    ChannelEcosystemEntry(
        "lark",
        "Lark",
        "messaging",
        "native_adapter",
        ("lark",),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Lark",
    ),
    ChannelEcosystemEntry(
        "feishu",
        "Feishu",
        "messaging",
        "native_adapter",
        ("feishu", "飞书"),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Feishu",
    ),
    ChannelEcosystemEntry(
        "line",
        "LINE",
        "messaging",
        "native_adapter",
        ("line",),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="LINE",
    ),
    ChannelEcosystemEntry(
        "slack",
        "Slack",
        "messaging",
        "native_adapter",
        ("slack",),
        True,
        True,
        ("signed_webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Slack",
        notes="Slack Events API inbound and chat.postMessage outbound.",
    ),
    ChannelEcosystemEntry(
        "discord",
        "Discord",
        "messaging",
        "native_adapter",
        ("discord",),
        True,
        True,
        ("signed_webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Discord",
        notes="Discord interactions or signed edge bridge inbound and bot REST outbound.",
    ),
    ChannelEcosystemEntry(
        "google_chat",
        "Google Chat",
        "messaging",
        "native_adapter",
        ("google chat", "gchat"),
        True,
        True,
        ("signed_webhook", "incoming_webhook", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Google Chat",
    ),
    ChannelEcosystemEntry(
        "signal",
        "Signal",
        "messaging",
        "native_adapter",
        ("signal",),
        True,
        True,
        ("signed_bridge", "signal_cli_rest", "ui_bridge"),
        _GROUP_CONTROLS + ("local_bridge_hardening",),
        risk="high",
        ui_bridge_app="Signal",
    ),
    ChannelEcosystemEntry(
        "imessage",
        "iMessage",
        "messaging",
        "native_adapter",
        ("imessage", "messages"),
        True,
        True,
        ("signed_local_relay", "foreground_confirmation", "ui_bridge"),
        _GROUP_CONTROLS + ("foreground_confirmation", "local_relay_hardening"),
        risk="high",
        ui_bridge_app="Messages",
    ),
    ChannelEcosystemEntry(
        "microsoft_teams",
        "Microsoft Teams",
        "messaging",
        "native_adapter",
        ("microsoft teams", "teams", "msteams"),
        True,
        True,
        ("bot_activity_webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        ui_bridge_app="Microsoft Teams",
    ),
    ChannelEcosystemEntry(
        "matrix",
        "Matrix",
        "messaging",
        "native_adapter",
        ("matrix",),
        True,
        True,
        ("signed_webhook", "homeserver_api"),
        _GROUP_CONTROLS,
    ),
    ChannelEcosystemEntry(
        "qq",
        "QQ",
        "messaging",
        "native_adapter",
        ("qq", "qq bot"),
        True,
        True,
        ("signed_webhook", "qq_bot_api", "ui_bridge"),
        _GROUP_CONTROLS,
        risk="high",
        ui_bridge_app="QQ",
    ),
    ChannelEcosystemEntry(
        "x",
        "X",
        "social",
        "native_adapter",
        ("x", "twitter", "x/twitter"),
        True,
        True,
        ("webhook", "outbound_queue", "ui_bridge"),
        _GROUP_CONTROLS,
        risk="high",
        ui_bridge_app="X",
    ),
    ChannelEcosystemEntry(
        "gmail",
        "Gmail",
        "email",
        "native_adapter",
        ("gmail", "email", "mail"),
        True,
        True,
        ("oauth", "pubsub_summary", "compose_gate"),
        _BASE_CONTROLS + ("oauth_scope_minimization",),
        risk="high",
        ui_bridge_app="Gmail",
    ),
    ChannelEcosystemEntry(
        "ui_bridge",
        "UI Bridge",
        "desktop",
        "ui_bridge",
        ("ui bridge", "desktop", "app ui", "visible ui"),
        True,
        True,
        ("foreground_confirmation", "visual_grounding"),
        ("foreground_confirmation", "permission_approval_gate", "audit_log"),
        risk="high",
    ),
)


OPENCLAW_REFERENCE_CHANNELS: tuple[ChannelEcosystemEntry, ...] = (
    ChannelEcosystemEntry("irc", "IRC", "messaging", "ecosystem_reference", ("irc",), True, True, ("reference_connector",), _GROUP_CONTROLS, source_reference="openclaw"),
    ChannelEcosystemEntry("mattermost", "Mattermost", "messaging", "ecosystem_reference", ("mattermost",), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, ui_bridge_app="Mattermost", source_reference="openclaw"),
    ChannelEcosystemEntry("nextcloud_talk", "Nextcloud Talk", "messaging", "ecosystem_reference", ("nextcloud talk", "nextcloud"), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, ui_bridge_app="Nextcloud Talk", source_reference="openclaw"),
    ChannelEcosystemEntry("nostr", "Nostr", "social", "ecosystem_reference", ("nostr",), True, True, ("reference_connector",), _GROUP_CONTROLS, risk="high", source_reference="openclaw"),
    ChannelEcosystemEntry("synology_chat", "Synology Chat", "messaging", "ecosystem_reference", ("synology chat", "synology"), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, ui_bridge_app="Synology Chat", source_reference="openclaw"),
    ChannelEcosystemEntry("tlon", "Tlon", "messaging", "ecosystem_reference", ("tlon", "urbit"), True, True, ("reference_connector",), _GROUP_CONTROLS, source_reference="openclaw"),
    ChannelEcosystemEntry("twitch", "Twitch", "social", "ecosystem_reference", ("twitch",), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, risk="high", ui_bridge_app="Twitch", source_reference="openclaw"),
    ChannelEcosystemEntry("zalo", "Zalo", "messaging", "ecosystem_reference", ("zalo",), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, ui_bridge_app="Zalo", source_reference="openclaw"),
    ChannelEcosystemEntry("zalo_personal", "Zalo Personal", "messaging", "ecosystem_reference", ("zalo personal",), True, True, ("reference_connector", "ui_bridge"), _GROUP_CONTROLS, ui_bridge_app="Zalo", source_reference="openclaw"),
    ChannelEcosystemEntry("webchat", "WebChat", "control_plane", "ecosystem_reference", ("webchat", "web chat"), True, True, ("reference_connector", "local_gateway"), ("origin_allowlist", "operator_pairing", "audit_log"), risk="high", source_reference="openclaw"),
    ChannelEcosystemEntry("voice_wake", "Voice Wake", "voice", "ecosystem_reference", ("voice wake", "talk mode", "voice", "wake word"), True, False, ("reference_node",), ("operator_pairing", "foreground_confirmation", "audit_log"), risk="high", source_reference="openclaw"),
    ChannelEcosystemEntry("live_canvas", "Live Canvas", "visual_workspace", "ecosystem_reference", ("live canvas", "canvas", "a2ui"), True, True, ("reference_visual_workspace",), ("operator_pairing", "permission_approval_gate", "audit_log"), risk="high", source_reference="openclaw"),
)


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", value.casefold()).strip()
    return {item for item in normalized.split() if item}


def channel_catalog(*, include_reference: bool = True) -> list[ChannelEcosystemEntry]:
    items = list(OMNIDESK_NATIVE_CHANNELS)
    if include_reference:
        items.extend(OPENCLAW_REFERENCE_CHANNELS)
    return sorted(items, key=lambda item: (item.status != "native_adapter", item.name))


def channel_matrix(*, include_reference: bool = True) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in channel_catalog(include_reference=include_reference)]


def resolve_channel(text: str, *, include_reference: bool = True) -> Optional[ChannelEcosystemEntry]:
    haystack = text.casefold()
    haystack_tokens = _tokens(text)
    best: Optional[ChannelEcosystemEntry] = None
    for entry in channel_catalog(include_reference=include_reference):
        candidates = (entry.name, entry.display_name, *entry.aliases)
        for candidate in candidates:
            needle = candidate.casefold()
            candidate_tokens = _tokens(candidate)
            if not needle:
                continue
            short_alias_match = len(needle) <= 2 and needle in haystack_tokens
            named_channel_match = len(needle) > 2 and (needle in haystack or candidate_tokens.issubset(haystack_tokens))
            if short_alias_match or named_channel_match:
                if best is None or best.status != "native_adapter" and entry.status == "native_adapter":
                    best = entry
                break
    return best


def recommend_surface(text: str, *, learned_surface: Optional[str] = None, include_reference: bool = True) -> dict[str, Any]:
    entry = resolve_channel(text, include_reference=include_reference)
    if entry is None:
        return {
            "target_channel": None,
            "display_name": None,
            "surface": learned_surface or "local_gateway",
            "ui_bridge_app": None,
            "status": "unknown",
            "risk": "medium",
            "required_controls": ("permission_approval_gate", "audit_log"),
            "source_reference": "omnidesk",
        }
    surface = learned_surface if learned_surface in entry.surfaces else entry.surfaces[0]
    return {
        "target_channel": entry.name,
        "display_name": entry.display_name,
        "surface": surface,
        "ui_bridge_app": entry.ui_bridge_app,
        "status": entry.status,
        "risk": entry.risk,
        "required_controls": entry.required_controls,
        "source_reference": entry.source_reference,
    }


def ecosystem_security_summary(*, include_reference: bool = True) -> dict[str, Any]:
    catalog = channel_catalog(include_reference=include_reference)
    controls = sorted({control for entry in catalog for control in entry.required_controls})
    return {
        "native_channel_count": sum(1 for entry in catalog if entry.status == "native_adapter"),
        "reference_channel_count": sum(1 for entry in catalog if entry.status == "ecosystem_reference"),
        "high_risk_channel_count": sum(1 for entry in catalog if entry.risk in {"high", "critical"}),
        "required_controls": controls,
        "security_model": "OmniDesk approval, allowlist, signature, sandbox, and audit controls remain authoritative.",
    }
