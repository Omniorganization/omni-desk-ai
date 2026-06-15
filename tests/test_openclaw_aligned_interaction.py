from __future__ import annotations

import asyncio

from omnidesk_agent.channels.ecosystem import channel_matrix, ecosystem_security_summary, resolve_channel
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.memory.experience import ExperienceStore


class _DummyLLM:
    pass


class _DummySkills:
    def prompt_block(self, text: str, max_chars: int = 6000) -> str:
        return ""


class _DummyTools:
    pass


def test_openclaw_aligned_channels_are_promoted_to_native_support():
    slack = resolve_channel("send this to Slack")
    telegram = resolve_channel("send this to Telegram")
    matrix = channel_matrix()
    summary = ecosystem_security_summary()

    assert slack is not None
    assert slack.status == "native_adapter"
    assert slack.source_reference == "omnidesk"
    assert telegram is not None
    assert telegram.status == "native_adapter"
    assert any(item["name"] == "discord" and item["status"] == "native_adapter" for item in matrix)
    assert "permission_approval_gate" in summary["required_controls"]
    assert "OmniDesk" in summary["security_model"]


def test_interaction_profile_persists_successful_channel_preference(tmp_path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        first = memory.record_interaction_profile(
            channel="local-api",
            actor="alice",
            task="please send a Telegram update",
            status="completed",
        )
        second = memory.record_interaction_profile(
            channel="local-api",
            actor="alice",
            task="send this to Slack",
            status="failed",
            manual_intervention=True,
        )
        loaded = memory.get_interaction_profile("local-api", "alice")

        assert first["preferred_channel"] == "telegram"
        assert second["task_count"] == 2
        assert second["preferred_channel"] == "telegram"
        assert loaded["failure_count"] == 1
        assert loaded["manual_intervention_count"] == 1
        assert 0 <= loaded["confidence"] <= 1


def test_planner_uses_native_ecosystem_signal_for_ui_bridge_target(tmp_path):
    async def run_case():
        with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
            planner = HierarchicalPlanner(_DummyLLM(), memory, _DummySkills(), _DummyTools())
            plan = await planner.plan(ChannelMessage(channel="local-api", sender_id="alice", text="在 Slack 通知团队今天的排期"))
            return plan

    plan = asyncio.run(run_case())

    assert plan.steps[0].tool == "ui_bridge"
    assert plan.steps[0].requires_approval is True
    assert plan.steps[0].args["app"] == "Slack"
    assert plan.steps[0].args["interaction_signal"]["status"] == "native_adapter"
    assert "交互入口" in plan.rationale
