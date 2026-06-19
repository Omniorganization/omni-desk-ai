from __future__ import annotations
from omnidesk_agent.tools.spec import ActionSpec, obj_schema


def test_action_spec_validates_required_and_type():
    spec = ActionSpec(
        "x",
        "x",
        obj_schema({"name": {"type": "string"}, "count": {"type": "integer"}}, required=["name"], additional=False),
    )
    assert spec.validate_args({"name": "a", "count": 1}) == []
    errors = spec.validate_args({"count": "bad", "extra": True})
    assert any("missing required" in e for e in errors)
    assert any("expected integer" in e for e in errors)
    assert any("unknown arg" in e for e in errors)
