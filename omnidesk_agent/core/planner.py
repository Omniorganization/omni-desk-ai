from __future__ import annotations

import json
import re
from typing import Any

from omnidesk_agent.core.llm import LLMClient
from omnidesk_agent.core.models import ChannelMessage, Plan, PlanStep
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.skills.registry import SkillRegistry
from omnidesk_agent.tools.registry import ToolRegistry


class HierarchicalPlanner:
    """Planner with experience retrieval.

    Production path: replace _rule_plan with a JSON-output LLM prompt. The model should
    return a high-level plan and executable atomic steps. This skeleton keeps deterministic
    local behavior so the repo can run without an API key.
    """

    def __init__(self, llm: LLMClient, memory: ExperienceStore, skills: SkillRegistry, tools: ToolRegistry):
        self.llm = llm
        self.memory = memory
        self.skills = skills
        self.tools = tools

    async def plan(self, msg: ChannelMessage) -> Plan:
        experiences = self.memory.search(msg.text, limit=4)
        return self._rule_plan(msg, experiences)

    def _rule_plan(self, msg: ChannelMessage, experiences: list[dict[str, Any]]) -> Plan:
        text = msg.text.strip()
        steps: list[PlanStep] = []
        lower = text.lower()
        rationale = "基于规则规划；已检索经验 {} 条。".format(len(experiences))

        if any(k in lower for k in ["screenshot", "screen", "屏幕", "看一下"]):
            steps.append(PlanStep("读取当前屏幕，建立视觉上下文", "computer", "screenshot", {"max_width": 1280}, "medium", True))
        if lower.startswith("shell:"):
            cmd = text.split(":", 1)[1].strip()
            steps.append(PlanStep("执行用户明确给出的 shell 命令", "shell", "run", {"command": cmd}, "critical", True))
        elif lower.startswith("写文件") or lower.startswith("write file"):
            steps.append(PlanStep("写入工作区文件", "files", "write_text", {"path": "note.txt", "text": text}, "high", True))
        elif any(k in lower for k in ["点击", "click"]):
            # Coordinates must be explicit to avoid blind clicking.
            m = re.search(r"(\d{2,5})\D+(\d{2,5})", text)
            if m:
                steps.append(PlanStep("点击用户指定坐标", "computer", "click", {"x": int(m.group(1)), "y": int(m.group(2))}, "high", True))
            else:
                steps.append(PlanStep("先截图确认可点击目标位置", "computer", "screenshot", {"max_width": 1280}, "medium", True))
        else:
            steps.append(PlanStep("记录任务并检索相关经验，等待更具体的执行目标", "files", "write_text", {"path": "inbox/latest_task.txt", "text": text}, "high", True))
        return Plan(goal=text, steps=steps, rationale=rationale)
