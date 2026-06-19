from __future__ import annotations
from omnidesk_agent.core.tool_selector import ToolSelector
from omnidesk_agent.core.plan_validator import PlanValidator
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema
from omnidesk_agent.core.plan_schema import StructuredPlan


class DummyTool:
    name = "browser"
    def spec(self):
        return ToolSpec(
            name="browser",
            description="browser",
            actions={
                "navigate": ActionSpec(
                    "navigate",
                    "navigate",
                    obj_schema({"url": {"type": "string"}}, required=["url"], additional=False),
                    risk="medium",
                    side_effect=True,
                    requires_approval=True,
                )
            },
        )


def test_tool_selector_reduces_context():
    tools = {name: {"name": name} for name in ["browser", "computer", "vision", "gmail", "shell", "files"]}
    selected = ToolSelector().select("open chrome and click button", tools)
    assert "browser" in selected
    assert "gmail" not in selected


def test_plan_validator_reuses_action_spec_validate_args():
    reg = ToolRegistry()
    reg.register(DummyTool())
    plan = StructuredPlan.model_validate({
        "goal": "g",
        "task_type": "browser",
        "risk": "medium",
        "steps": [{
            "description": "nav",
            "tool": "browser",
            "action": "navigate",
            "args": {"url": 123},
            "risk": "medium",
            "requires_approval": True,
            "expected_result": "page opens"
        }],
        "success_criteria": "opened",
        "rollback_plan": "none"
    })
    result = PlanValidator(reg).validate(plan)
    assert not result.ok
    assert "expected string" in "\\n".join(result.errors)
