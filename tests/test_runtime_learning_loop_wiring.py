from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime


def test_runtime_wires_learning_loop_into_orchestrator(tmp_path: Path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.llm.provider = "rule"
    cfg.channels.chrome.enabled = False
    cfg.plugins.enabled = False
    cfg.memory_privacy.encrypt_at_rest = False
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.ensure_dirs()

    rt = OmniDeskRuntime(cfg)
    try:
        assert rt.learning_loop is not None
        assert rt.orchestrator.learning_loop is rt.learning_loop
        # agent-core's orchestrator uses the hook protocol, so the policy is applied.
        assert callable(rt.learning_loop.assign) and callable(rt.learning_loop.observe)
        assert rt.healing.mode == "observe"  # audits decisions, changes nothing by default

        msg = ChannelMessage(channel="telegram", sender_id="u1", text="price?", thread_id="t1")
        assert rt.learning_loop.assign(msg) is None  # nothing learned yet: runs are untouched
        registry = rt.learning_runtime.registry
        artifact = registry.register_candidate("skill", "price", "Use browser.extract for prices.")
        registry.record_evaluation(artifact.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 12})
        approval = registry.request_approval(artifact.artifact_id)
        registry.decide(approval.approval_id, "approved", reviewer="owner@example.com")
        registry.apply(artifact.artifact_id, actor="owner@example.com")
        assigned = rt.learning_loop.assign(msg)
        assert assigned is not None and "browser.extract" in assigned.policy.skills_block
    finally:
        rt.close()
