from __future__ import annotations

import json
from typing import Any


class ToolSelector:
    """Reduce planner context by selecting only relevant tools."""

    BUCKETS = {
        "gmail": {"gmail", "files", "channels", "channel_send"},
        "email": {"gmail", "files", "channels", "channel_send"},
        "chrome": {"browser", "computer", "vision", "ui_bridge"},
        "browser": {"browser", "computer", "vision", "ui_bridge"},
        "screen": {"computer", "vision", "ui_bridge"},
        "屏幕": {"computer", "vision", "ui_bridge"},
        "click": {"computer", "vision", "ui_bridge"},
        "点击": {"computer", "vision", "ui_bridge"},
        "shell": {"shell", "git", "test", "files", "pull_request"},
        "git": {"shell", "git", "test", "files", "pull_request"},
        "代码": {"shell", "git", "test", "files", "pull_request"},
        "file": {"files", "shell"},
        "文件": {"files", "shell"},
        "whatsapp": {"channels", "channel_send", "ui_bridge"},
        "telegram": {"channels", "channel_send"},
        "wechat": {"channels", "channel_send", "ui_bridge"},
        "微信": {"channels", "channel_send", "ui_bridge"},
        "facebook": {"browser", "ui_bridge", "computer", "vision"},
        "instagram": {"browser", "ui_bridge", "computer", "vision"},
        "小红书": {"browser", "ui_bridge", "computer", "vision", "files"},
    }

    ALWAYS = {"approval", "memory"}

    def select(self, task: str, tool_descriptions: dict[str, Any], max_tools: int = 8) -> dict[str, Any]:
        if not tool_descriptions:
            return {}
        lower = task.lower()
        selected: set[str] = set()
        for key, tools in self.BUCKETS.items():
            if key in lower:
                selected.update(tools)
        selected.update(t for t in self.ALWAYS if t in tool_descriptions)

        if not selected:
            # Keep context small but non-empty for generic tasks.
            preferred = ["files", "browser", "computer", "vision", "gmail", "shell", "ui_bridge", "channel_send"]
            selected.update([t for t in preferred if t in tool_descriptions][:max_tools])

        filtered = {name: tool_descriptions[name] for name in sorted(selected) if name in tool_descriptions}
        if not filtered:
            return dict(list(tool_descriptions.items())[:max_tools])
        return dict(list(filtered.items())[:max_tools])

    def to_json(self, task: str, tool_descriptions: dict[str, Any], max_tools: int = 8) -> str:
        return json.dumps(self.select(task, tool_descriptions, max_tools=max_tools), ensure_ascii=False, indent=2)
