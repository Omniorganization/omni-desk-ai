from __future__ import annotations

import json

from pydantic import ValidationError

from omnidesk_agent.core.models import ChannelMessage, Plan
from omnidesk_agent.core.plan_schema import StructuredPlan
from omnidesk_agent.core.plan_validator import PlanValidator
from omnidesk_agent.models.base import ModelRequest
from omnidesk_agent.models.router import ModelRouter
from omnidesk_agent.core.tool_selector import ToolSelector


class LLMStructuredPlanner:
    """LLM planner that emits strict StructuredPlan JSON and validates it before execution."""

    def __init__(self, router: ModelRouter, memory, skills, tools, fallback_planner):
        self.router = router
        self.memory = memory
        self.skills = skills
        self.tools = tools
        self.fallback_planner = fallback_planner
        self.validator = PlanValidator(tools)
        self.tool_selector = ToolSelector()

    async def plan(self, msg: ChannelMessage) -> Plan:
        # Keep rule planner for obvious single-step commands and as fallback.
        if self._should_use_rule(msg.text):
            return await self.fallback_planner.plan(msg)

        experiences = self.memory.retrieve_for_task(msg.text, limit=4) if hasattr(self.memory, 'retrieve_for_task') else self.memory.search(msg.text, limit=4)
        skills_context = self.skills.prompt_block(msg.text, max_chars=6000)
        all_tools = self.tools.describe() if hasattr(self.tools, "describe") else {}
        selected_tools = self.tool_selector.select(msg.text, all_tools)
        tools_context = json.dumps(selected_tools, ensure_ascii=False, indent=2)

        system = (
            "You are Omni-deskAi's structured planner. Return only valid JSON matching this schema: "
            "{goal, task_type, risk, steps, success_criteria, rollback_plan}. "
            "Every step requires: description, tool, action, args, risk, requires_approval, expected_result. "
            "Use only available tools/actions. Avoid unnecessary screenshots or model calls. "
            "For external sending, use channels/gmail tools and require approval. "
            "For UI work, observe first, then vision.ground if an image_path is available, then computer click/type only after target confidence."
        )
        user = json.dumps(
            {
                "task": msg.text,
                "channel": msg.channel,
                "available_tools": json.loads(tools_context),
                "skills": skills_context,
                "recent_experiences": experiences,
            },
            ensure_ascii=False,
        )

        raw = await self.router.complete(ModelRequest(
            system=system,
            user=user,
            task="planner",
            json_mode=True,
            verified_required=True,
            task_id=f"planner-{msg.message_id or msg.thread_id}",
        ))

        try:
            structured = StructuredPlan.model_validate_json(raw.text)
        except (ValidationError, json.JSONDecodeError, ValueError):
            repaired = await self._repair(raw.text, tools_context, msg)
            try:
                structured = StructuredPlan.model_validate_json(repaired)
            except Exception:
                return await self.fallback_planner.plan(msg)

        result = self.validator.validate(structured)
        if not result.ok or result.plan is None:
            return await self.fallback_planner.plan(msg)

        return PlanValidator.to_runtime_plan(result.plan, rationale="LLM structured plan validated")

    async def _repair(self, bad_json: str, tools_context: str, msg: ChannelMessage) -> str:
        system = "Repair invalid plan JSON. Return only valid JSON matching the StructuredPlan schema."
        user = json.dumps({"bad_json": bad_json[:12000], "task": msg.text, "available_tools": json.loads(tools_context)}, ensure_ascii=False)
        resp = await self.router.complete(ModelRequest(
            system=system,
            user=user,
            task="planner",
            json_mode=True,
            verified_required=True,
            task_id=f"planner-repair-{msg.message_id or msg.thread_id}",
        ))
        return resp.text

    @staticmethod
    def _should_use_rule(text: str) -> bool:
        lower = text.lower().strip()
        return lower.startswith("shell:") or lower.startswith("write file") or lower.startswith("写文件")
