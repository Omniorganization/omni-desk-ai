from __future__ import annotations

import pytest

from omnidesk_agent.core.models import ApprovalDecision, ToolResult
from omnidesk_agent.core.vision_executor import VisionActionExecutor
from omnidesk_agent.tools.base import ToolContext


class Permissions:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.proposals = []

    def verify(self, proposal):
        self.proposals.append(proposal)
        return ApprovalDecision(self.allowed, "allow" if self.allowed else "deny", "test")


class ToolCalls:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, tool, action, args, ctx):
        self.calls.append((tool, action, args))
        return self.responses.pop(0)


def ctx(allowed=True):
    return ToolContext(permissions=Permissions(allowed), source="test", actor="u")


@pytest.mark.asyncio
async def test_vision_executor_low_confidence_no_ctx_returns_approval_payload():
    tools = ToolCalls([])
    executor = VisionActionExecutor(tools, min_click_confidence=0.8)
    result = await executor.maybe_click_target(
        ToolResult(True, data={"grounding": {"target": {"x": 10, "y": 20, "width": 10, "height": 10, "confidence": 0.3}}}),
        "click target",
        None,
        {"image_path": "/tmp/s.png", "scale_ratio": 2.0, "window_title": "W"},
    )
    assert result is not None
    assert result.ok is False
    assert result.data["click_args"]["x"] == 7
    assert result.data["click_args"]["vision_evidence"]["screenshot"]["window_title"] == "W"


@pytest.mark.asyncio
async def test_vision_executor_low_confidence_denied_and_high_confidence_executes():
    executor = VisionActionExecutor(ToolCalls([]), min_click_confidence=0.8)
    denied = await executor.maybe_click_target(
        ToolResult(True, data={"grounding": {"target": {"x": 1, "y": 2, "confidence": 0.2}}}),
        "click",
        ctx(False),
    )
    assert denied is not None and denied.ok is False

    click_result = ToolResult(True, summary="clicked")
    tools = ToolCalls([click_result])
    executor = VisionActionExecutor(tools, min_click_confidence=0.5)
    result = await executor.maybe_click_target(
        ToolResult(True, data={"grounding": {"confidence": 0.9, "target": {"x": 1, "y": 2}}}),
        "click",
        ctx(True),
    )
    assert result is click_result
    assert tools.calls[0][0:2] == ("computer", "click")


@pytest.mark.asyncio
async def test_vision_executor_verify_with_retry_success_and_failure(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("omnidesk_agent.core.vision_executor.asyncio.sleep", no_sleep)
    ok_tools = ToolCalls([
        ToolResult(True, data={"image_path": "screen.png"}),
        ToolResult(True, data={"grounding": {"target": {"confidence": 0.9}}}),
    ])
    executor = VisionActionExecutor(ok_tools)
    passed = await executor.verify_with_retry(
        ctx=ctx(),
        instruction="done",
        verification={"expected": "done", "min_confidence": 0.8},
        retry_policy={"max_retries": 0},
    )
    assert passed.ok is True

    fail_tools = ToolCalls([
        ToolResult(False, error="no shot"),
        ToolResult(True, data={"image_path": "screen.png"}),
        ToolResult(True, data={"grounding": {"confidence": 0.1}}),
    ])
    executor = VisionActionExecutor(fail_tools)
    failed = await executor.verify_with_retry(
        ctx=ctx(),
        instruction="done",
        verification={"expected": "done", "min_confidence": 0.8},
        retry_policy={"max_retries": 1, "backoff_seconds": 0},
    )
    assert failed.ok is False
    assert failed.data["last_result"]["ok"] is True


def test_vision_executor_static_helpers_cover_edge_cases():
    assert VisionActionExecutor._target(ToolResult(True, data={"grounding": []})) is None
    assert VisionActionExecutor._target(ToolResult(True, data={"grounding": {}})) is None
    assert VisionActionExecutor._verified(ToolResult(False), 0.1) is False
    assert VisionActionExecutor._verified(ToolResult(True, data={"grounding": {"verified": True}}), 0.99) is True
    assert VisionActionExecutor._safe_result(None) is None
    assert VisionActionExecutor._safe_result(ToolResult(False, error="e", summary="s", data={"x": 1}))["error"] == "e"
