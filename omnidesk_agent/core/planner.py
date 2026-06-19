from __future__ import annotations
import re
from typing import Any, Optional, cast
from omnidesk_agent.core.llm import LLM
from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep
from omnidesk_agent.channels.ecosystem import resolve_channel
from omnidesk_agent.learning.interaction_profile import InteractionSignal, infer_interaction_signal
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.skills.registry import SkillRegistry
from omnidesk_agent.tools.registry import ToolRegistry

class HierarchicalPlanner:
    def __init__(self, llm: LLM, memory: ExperienceStore, skills: SkillRegistry, tools: ToolRegistry):
        self.llm = llm
        self.memory = memory
        self.skills = skills
        self.tools = tools

    async def plan(self, msg: ChannelMessage) -> Plan:
        experiences = self.memory.search(msg.text, limit=4)
        skills_context = self.skills.prompt_block(msg.text, max_chars=6000)
        profile: Optional[dict[str, Any]] = None
        get_profile = getattr(self.memory, "get_interaction_profile", None)
        if callable(get_profile):
            try:
                candidate = get_profile(msg.channel, msg.sender_id)
                if isinstance(candidate, dict):
                    profile = candidate
            except Exception:
                profile = None
        interaction = infer_interaction_signal(msg.text, source_channel=msg.channel, actor=msg.sender_id, learned_profile=profile)
        return self._rule_plan(msg, experiences, skills_context, interaction)

    def _rule_plan(self, msg: ChannelMessage, experiences: list[dict[str, Any]], skills_context: str, interaction: Optional[InteractionSignal] = None) -> Plan:
        if interaction is None:
            interaction = infer_interaction_signal(msg.text, source_channel=msg.channel, actor=msg.sender_id)
        text = msg.text.strip()
        lower = text.lower()
        steps: list[PlanStep] = []
        skill_note = "；已使用匹配 Skills" if skills_context else "；未匹配 Skills"
        interaction_note = f"；交互入口={interaction.surface}"
        if interaction.target_channel:
            interaction_note += f"/{interaction.target_channel}"
        if interaction.learned_profile_used:
            interaction_note += "；已使用交互画像"
        rationale = "基于规则规划；已检索经验 {} 条{}{}".format(len(experiences), skill_note, interaction_note)
        screenshot_args = {"max_width": 960, "expected_result": "确认当前屏幕状态是否与任务目标相关", "skip_if_unchanged": True, "skip_if_too_soon": True}
        if skills_context:
            screenshot_args["skills_context_used"] = True
        if any(k in lower for k in ["screenshot", "screen", "屏幕", "看一下"]):
            steps.append(PlanStep("读取当前屏幕，建立视觉上下文", "computer", "screenshot", screenshot_args, "medium", True))
        if lower.startswith("shell:"):
            cmd = text.split(":", 1)[1].strip()
            steps.append(PlanStep("执行用户明确给出的 shell 命令", "shell", "run", {"command": cmd, "expected_result": "完成用户明确指定的 shell 命令"}, "critical", True))
        elif lower.startswith("写文件") or lower.startswith("write file"):
            steps.append(PlanStep("写入工作区文件", "files", "write_text", {"path": "note.txt", "text": text, "expected_result": "在工作区保存用户指定内容"}, "high", True))
        elif any(k in lower for k in ["点击", "click"]):
            m = re.search(r"(\d{2,5})\D+(\d{2,5})", text)
            if m:
                steps.append(PlanStep("点击用户指定坐标", "computer", "click", {"x": int(m.group(1)), "y": int(m.group(2)), "expected_result": "点击用户指定坐标并观察结果"}, "high", True))
            else:
                steps.append(PlanStep("先截图确认可点击目标位置", "computer", "screenshot", screenshot_args, "medium", True))
        elif interaction.target_channel or any(k in lower for k in ["whatsapp", "微信", "wechat", "小红书", "xiaohongshu", "gmail", "chrome", "instagram", "facebook", "telegram", "line", "钉钉", "dingtalk", "飞书", "feishu", "lark", "x/twitter", "twitter"]):
            app = interaction.ui_bridge_app or self._guess_app(text)
            risk = cast(Any, interaction.risk if interaction.risk in {"low", "medium", "high", "critical"} else "medium")
            steps.append(PlanStep(
                "通过可见 UI Bridge 或官方渠道接入口确认目标应用状态",
                "ui_bridge",
                "observe",
                {
                    "app": app,
                    "expected_result": "确认目标应用是否可见并准备执行下一步",
                    "interaction_signal": interaction.to_dict(),
                },
                risk,
                True,
            ))
        else:
            steps.append(PlanStep("记录任务并检索相关经验，等待更具体的执行目标", "files", "write_text", {"path": "inbox/latest_task.txt", "text": text, "expected_result": "保存待处理任务"}, "high", True))
        return Plan(goal=text, steps=steps, rationale=rationale)

    @staticmethod
    def _guess_app(text: str) -> str:
        channel = resolve_channel(text)
        if channel and channel.ui_bridge_app:
            return channel.ui_bridge_app
        lower = text.lower()
        mapping = {"whatsapp business": "WhatsApp Business", "whatsapp": "WhatsApp", "wechat": "WeChat", "微信": "WeChat", "dingtalk": "DingTalk", "钉钉": "DingTalk", "lark": "Lark", "feishu": "Feishu", "飞书": "Feishu", "小红书": "Xiaohongshu", "xiaohongshu": "Xiaohongshu", "line": "LINE", "telegram": "Telegram", "facebook": "Facebook", "instagram": "Instagram", "gmail": "Gmail", "chrome": "Google Chrome", "x/twitter": "X", "twitter": "X"}
        for k, v in mapping.items():
            if k in lower:
                return v
        return "Google Chrome"
