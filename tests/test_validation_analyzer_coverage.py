from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.self_learning.skill_learning.retirement import SkillRetirementPolicy
from omnidesk_agent.self_upgrade.analyzer import UpgradeAnalyzer
from omnidesk_agent.validation.connectors import REQUIRED_APPS, validate_connectors
from omnidesk_agent.validation.models import live_connectivity_test, validate_models


class Tools:
    def names(self):
        return ["ui_bridge", "browser", "gmail"]


class Router:
    def __init__(self):
        self.cfg = SimpleNamespace(routing={k: k for k in ["planner", "tool_plan", "chat", "code", "vision", "private", "summarize", "upgrade"]})
        self.providers = {k: SimpleNamespace(settings=SimpleNamespace(api_key_env=None)) for k in self.cfg.routing}
        self.calls = []

    def status(self):
        return {"profiles": sorted(self.providers)}

    async def complete(self, request):
        self.calls.append(request)
        return SimpleNamespace(text="ok", provider="fake", model="m")


def test_validate_connectors_and_models_and_live_connectivity(tmp_path):
    cfg = SimpleNamespace(channels=SimpleNamespace(ui_bridge=SimpleNamespace(allowed_apps=list(REQUIRED_APPS))))
    runtime = SimpleNamespace(
        adapters={"whatsapp_cloud": object(), "wechat_official": object(), "dingtalk": object(), "lark": object(), "feishu": object(), "line": object(), "x": object(), "telegram": object(), "meta_graph": object(), "gmail": object()},
        tools=Tools(),
        cfg=cfg,
        model_router=Router(),
    )
    connector_result = validate_connectors(runtime)
    assert connector_result["ok"] is True
    assert connector_result["coverage"]["Gmail"] is True

    model_result = validate_models(runtime)
    assert model_result["ok"] is True
    assert "openai" in model_result["supported_provider_aliases"]

    live = asyncio.run(live_connectivity_test(runtime, profiles=["chat", "missing"]))
    assert live["ok"] is True
    assert live["results"]["missing"]["error"] == "profile not loaded"


def test_upgrade_analyzer_report_and_skill_retirement(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        "\n".join([
            json.dumps({"decision": "allow", "tool": "files", "risk": "low"}),
            json.dumps({"decision": "deny", "tool": "shell", "action": "run", "risk": "critical", "reason": "blocked"}),
            "not-json",
        ]),
        encoding="utf-8",
    )
    memory = ExperienceStore(tmp_path / "memory.sqlite3")
    try:
        memory.add("previous error failed PermissionDenied", outcome="failed")
        report = UpgradeAnalyzer(audit, memory).build_report(limit=20)
        assert "Audit Decisions" in report
        assert "shell.run" in report
        assert "previous error" in report
    finally:
        memory.close()

    policy = SkillRetirementPolicy()
    assert policy.should_retire(bad_memory_rate=0.4)
    assert policy.should_retire(replay_regression=0.3)
    assert not policy.should_retire(bad_memory_rate=0.1, replay_regression=0.1)
