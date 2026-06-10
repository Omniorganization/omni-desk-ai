from omnidesk_agent.core.plan_schema import StructuredPlan
from omnidesk_agent.core.plan_validator import PlanValidator


class DummyTools:
    def names(self):
        return ["files"]

    def describe(self):
        return {
            "files": {
                "actions": {
                    "write_text": {
                        "description": "write",
                        "input_schema": {},
                        "risk": "high",
                        "side_effect": True,
                        "requires_approval": True,
                    }
                }
            }
        }


def test_plan_validator_accepts_valid_plan():
    plan = StructuredPlan.model_validate({
        "goal": "write note",
        "task_type": "file",
        "risk": "high",
        "success_criteria": "file written",
        "steps": [{
            "description": "write file",
            "tool": "files",
            "action": "write_text",
            "args": {"path": "note.txt", "text": "hi"},
            "risk": "high",
            "requires_approval": True,
            "expected_result": "note is saved"
        }]
    })
    result = PlanValidator(DummyTools()).validate(plan)
    assert result.ok
    assert plan.steps[0].args["expected_result"] == "note is saved"


def test_plan_validator_rejects_unknown_tool():
    plan = StructuredPlan.model_validate({
        "goal": "bad",
        "task_type": "unknown",
        "risk": "medium",
        "success_criteria": "none",
        "steps": [{
            "description": "bad",
            "tool": "missing",
            "action": "x",
            "args": {},
            "risk": "medium",
            "requires_approval": True,
            "expected_result": "x"
        }]
    })
    result = PlanValidator(DummyTools()).validate(plan)
    assert not result.ok
