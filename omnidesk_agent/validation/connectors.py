from __future__ import annotations
REQUIRED_APPS = ["WhatsApp", "WhatsApp Business", "WeChat", "DingTalk", "Lark", "Feishu", "Slack", "Discord", "Google Chat", "Signal", "Messages", "Microsoft Teams", "Matrix", "QQ", "Xiaohongshu", "LINE", "X", "Telegram", "Facebook", "Instagram", "Google Chrome", "Gmail"]

def validate_connectors(runtime) -> dict:
    adapters = runtime.adapters
    tools = set(runtime.tools.names())
    ui_apps = set(runtime.cfg.channels.ui_bridge.allowed_apps)
    coverage = {
        "WhatsApp": "WhatsApp" in ui_apps and "ui_bridge" in tools,
        "WhatsApp Business": "whatsapp_cloud" in adapters,
        "WeChat": "wechat_official" in adapters or "WeChat" in ui_apps,
        "DingTalk": "dingtalk" in adapters or "DingTalk" in ui_apps,
        "Lark": "lark" in adapters or "Lark" in ui_apps,
        "Feishu": "feishu" in adapters or "Feishu" in ui_apps,
        "Slack": "slack" in adapters or "Slack" in ui_apps,
        "Discord": "discord" in adapters or "Discord" in ui_apps,
        "Google Chat": "google_chat" in adapters or "Google Chat" in ui_apps,
        "Signal": "signal" in adapters or "Signal" in ui_apps,
        "Messages": "imessage" in adapters or "Messages" in ui_apps,
        "Microsoft Teams": "microsoft_teams" in adapters or "Microsoft Teams" in ui_apps,
        "Matrix": "matrix" in adapters or "Matrix" in ui_apps,
        "QQ": "qq" in adapters or "QQ" in ui_apps,
        "Xiaohongshu": "Xiaohongshu" in ui_apps and "ui_bridge" in tools,
        "LINE": "line" in adapters or "LINE" in ui_apps,
        "X": "x" in adapters or "X" in ui_apps,
        "Telegram": "telegram" in adapters or "Telegram" in ui_apps,
        "Facebook": "meta_graph" in adapters or "Facebook" in ui_apps,
        "Instagram": "meta_graph" in adapters or "Instagram" in ui_apps,
        "Google Chrome": "browser" in tools or "Google Chrome" in ui_apps,
        "Gmail": "gmail" in adapters or "gmail" in tools or "Gmail" in ui_apps,
    }
    return {
        "ok": all(coverage.values()),
        "coverage": coverage,
        "direct_adapters": sorted(adapters.keys()),
        "tools": sorted(tools),
        "ui_bridge_allowed_apps": sorted(ui_apps),
        "notes": {
            "personal_account_apps": "WhatsApp personal, WeChat personal, Xiaohongshu and personal social apps use visible UI Bridge with human approval, not unofficial reverse-engineered APIs.",
            "official_api_apps": "WhatsApp Business, Telegram, LINE, DingTalk, Lark/Feishu, Slack, Discord, Google Chat, Teams, Matrix, QQ, Meta/Facebook/Instagram, X, Gmail and Chrome use official or signed bridge APIs where configured.",
        },
    }
