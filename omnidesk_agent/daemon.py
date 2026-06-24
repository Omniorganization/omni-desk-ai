from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.channels.dingtalk import DingTalkChannel
from omnidesk_agent.channels.gmail import GmailChannel
from omnidesk_agent.channels.lark_feishu import FeishuChannel, LarkChannel
from omnidesk_agent.channels.line import LineChannel
from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.channels.native_messaging import (
    DiscordChannel,
    GoogleChatChannel,
    IMessageChannel,
    MatrixChannel,
    MicrosoftTeamsChannel,
    QQChannel,
    SignalChannel,
    SlackChannel,
)
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.channels.wechat_official import WeChatOfficialChannel
from omnidesk_agent.channels.x_channel import XChannel
from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy
from omnidesk_agent.core.llm import RouterLLMAdapter, RuleBasedLLM
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.outbound_dispatcher import OutboundDispatcher
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.core.structured_planner import LLMStructuredPlanner
from omnidesk_agent.core.token_budget import TokenBudgetConfig
from omnidesk_agent.core.worker import WebhookWorker
from omnidesk_agent.learning.daily_job import DailySelfLearningJob
from omnidesk_agent.models.router import build_model_router
from omnidesk_agent.plugins.registry import PluginRegistry
from omnidesk_agent.repositories.runtime import build_repository_factory, storage_plan
from omnidesk_agent.security.admin_auth import AdminAuth
from omnidesk_agent.security.audit_worm import WormAuditCheckpoint
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.self_learning.runtime_loop import RuntimeLearningLoop
from omnidesk_agent.self_upgrade.governance import GovernedSelfImprovement
from omnidesk_agent.skills.registry import SkillRegistry
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.test_tool import TestTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool
from omnidesk_agent.tools.vision import VisionGroundingTool


class OmniDeskRuntime:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.metrics: Any = None
        self.otel_exporter: Any = None
        self.storage_plan = storage_plan(backend=cfg.storage.backend, require_multi_instance_safe=cfg.storage.require_multi_instance_safe)
        self.repository_factory = build_repository_factory(backend=cfg.storage.backend, workspace_root=cfg.workspace.root, postgres_dsn_env=cfg.storage.postgres_dsn_env)
        self.transactional_outbox = self.repository_factory.transactional_outbox()
        self.dual_approval_store = self.repository_factory.dual_approval_store()
        self.approval_store = self.repository_factory.approval_store(
            ttl_seconds=cfg.permissions.approval_ttl_seconds,
            dual_approval_store=self.dual_approval_store,
        )
        self.break_glass_store = self.repository_factory.break_glass_store(audit_log=cfg.permissions.audit_log)
        self.audit_checkpoint = WormAuditCheckpoint(cfg.workspace.root / "audit_checkpoints", hmac_key_env=cfg.permissions.audit_checkpoint_hmac_key_env)
        self.permissions = PermissionManager(cfg.permissions, self.approval_store)
        self.webhook_security = self.repository_factory.webhook_security()
        self.job_queue = self.repository_factory.job_queue()
        self.outbound_messages = self.repository_factory.outbound_messages()
        self.learning_experiments = self.repository_factory.learning_experiments()
        self.learning_loop = RuntimeLearningLoop(self.learning_experiments)
        self.admin_auth = AdminAuth(
            admin_token_env=cfg.gateway.admin_token_env,
            viewer_token_env=cfg.gateway.viewer_token_env,
            operator_token_env=cfg.gateway.operator_token_env,
            owner_token_env=cfg.gateway.owner_token_env,
            legacy_secret_env=cfg.gateway.shared_secret_env,
            allow_local_without_token=cfg.gateway.allow_local_admin_without_token,
            allowed_ips=cfg.gateway.admin_allowed_ips,
            audit_log=cfg.workspace.root / "admin_auth_audit.jsonl",
            break_glass_store=self.break_glass_store,
            break_glass_enabled=cfg.permissions.break_glass_enabled,
        )
        self.memory = self.repository_factory.memory_store(cfg.memory_privacy)
        self.token_budget = self.repository_factory.token_budget_manager(
            TokenBudgetConfig(
                max_input_chars=cfg.llm.max_input_chars,
                max_output_tokens=cfg.llm.max_output_tokens,
                per_task_max_llm_calls=cfg.llm.per_task_max_llm_calls,
                cache_ttl_seconds=cfg.llm.cache_ttl_seconds,
                enable_cache=cfg.llm.enable_cache,
                require_approval_above_estimated_tokens=cfg.llm.require_approval_above_estimated_tokens,
            )
        )
        self.execution_strategy = ResultOrientedExecutionStrategy()
        self.run_store = self.repository_factory.run_store()
        self.agent_run_idempotency = self.repository_factory.agent_run_idempotency_store()
        self.side_effect_idempotency = self.repository_factory.side_effect_idempotency_store()
        self.skills = SkillRegistry(cfg.workspace.skills_dirs)
        self.plugins = PluginRegistry(cfg.workspace.plugins_dirs, cfg.plugins)
        self.tools = ToolRegistry()
        self.adapters = self._build_channel_adapters()
        self.model_cost_store = self.repository_factory.model_cost_store()
        self.model_router = build_model_router(
            cfg.models,
            self.token_budget,
            self.model_cost_store,
            require_persistent_ledger=cfg.models.budget.require_persistent_ledger,
        )
        self._register_builtin_tools()
        self.skills.load()
        if cfg.plugins.enabled and cfg.capabilities.plugins.enabled:
            self.plugins.load_into(self.tools, cfg)
        llm = RuleBasedLLM() if cfg.llm.provider == "rule" else RouterLLMAdapter(self.model_router, task="planner")
        self.rule_planner = HierarchicalPlanner(llm=llm, memory=self.memory, skills=self.skills, tools=self.tools)
        self.planner = self.rule_planner if cfg.llm.provider == "rule" else LLMStructuredPlanner(self.model_router, self.memory, self.skills, self.tools, self.rule_planner)
        self.orchestrator = Orchestrator(
            self.planner,
            self.tools,
            self.permissions,
            self.memory,
            self.execution_strategy,
            self.run_store,
            self.approval_store,
            learning_loop=self.learning_loop,
            dual_approval_store=self.dual_approval_store,
        )
        self.orchestrator.storage_plan = self.storage_plan
        self.webhook_worker: Optional[WebhookWorker] = WebhookWorker(self.job_queue, self.orchestrator)
        self.outbound_dispatcher: Optional[OutboundDispatcher] = OutboundDispatcher(self.outbound_messages, self.adapters)
        self.learning_job = DailySelfLearningJob(self.memory, cfg.workspace.root)
        self.governance = GovernedSelfImprovement(cfg.workspace.root, Path.cwd(), sandbox_cfg=cfg.sandbox)
        self.proposal_store = self.governance.proposal_store

    def _build_channel_adapters(self) -> dict:
        return {
            "telegram": TelegramChannel(self.cfg.channels.telegram),
            "whatsapp_cloud": WhatsAppCloudChannel(self.cfg.channels.whatsapp_cloud),
            "wechat_official": WeChatOfficialChannel(self.cfg.channels.wechat_official),
            "meta_graph": MetaGraphChannel(self.cfg.channels.meta_graph),
            "dingtalk": DingTalkChannel(self.cfg.channels.dingtalk),
            "lark": LarkChannel(self.cfg.channels.lark),
            "feishu": FeishuChannel(self.cfg.channels.feishu),
            "line": LineChannel(self.cfg.channels.line),
            "x": XChannel(self.cfg.channels.x),
            "slack": SlackChannel(self.cfg.channels.slack),
            "discord": DiscordChannel(self.cfg.channels.discord),
            "google_chat": GoogleChatChannel(self.cfg.channels.google_chat),
            "signal": SignalChannel(self.cfg.channels.signal),
            "imessage": IMessageChannel(self.cfg.channels.imessage),
            "microsoft_teams": MicrosoftTeamsChannel(self.cfg.channels.microsoft_teams),
            "matrix": MatrixChannel(self.cfg.channels.matrix),
            "qq": QQChannel(self.cfg.channels.qq),
            # Convenience aliases keep user-facing channel names predictable while
            # preserving the existing canonical adapter keys used by storage.
            "whatsapp": WhatsAppCloudChannel(self.cfg.channels.whatsapp_cloud),
            "wechat": WeChatOfficialChannel(self.cfg.channels.wechat_official),
            "teams": MicrosoftTeamsChannel(self.cfg.channels.microsoft_teams),
            "gmail": GmailChannel(self.cfg.channels.gmail),
        }

    def _register_builtin_tools(self) -> None:
        # Register the smallest runtime capability surface. Tools still keep
        # their own internal gates; this layer prevents disabled capabilities
        # from being visible to planners in the first place.
        caps = self.cfg.capabilities
        if caps.files.enabled:
            self.tools.register(FilesTool(self.cfg.workspace.root))
        if caps.git.enabled:
            self.tools.register(GitTool(Path.cwd()))
        if caps.test.enabled:
            self.tools.register(TestTool(Path.cwd()))
        if caps.shell.enabled:
            self.tools.register(ShellTool(self.cfg.workspace.root, self.cfg.permissions, self.cfg.sandbox))
        if caps.computer.enabled:
            self.tools.register(ComputerTool(self.cfg.workspace.root / "screenshots"))
        if caps.vision.enabled:
            self.tools.register(VisionGroundingTool(self.model_router))
        if caps.pull_request.enabled:
            self.tools.register(PullRequestTool(Path.cwd()))

        if caps.channels.enabled and self._any_outbound_channel_enabled():
            self.tools.register(ChannelSendTool(self.adapters, self.outbound_messages))
        if caps.ui_bridge.enabled and self.cfg.channels.ui_bridge.enabled:
            self.tools.register(UIBridgeTool(self.cfg.channels.ui_bridge, self.tools))
        if caps.browser.enabled and self.cfg.channels.chrome.enabled:
            self.tools.register(BrowserTool(self.cfg.channels.chrome))
        if caps.gmail.enabled and self.cfg.channels.gmail.enabled:
            self.tools.register(GmailTool(self.adapters["gmail"]))

    def _any_outbound_channel_enabled(self) -> bool:
        for name in (
            "telegram", "whatsapp_cloud", "wechat_official", "meta_graph", "dingtalk",
            "lark", "feishu", "line", "x", "slack", "discord", "google_chat",
            "signal", "imessage", "microsoft_teams", "matrix", "qq",
        ):
            if bool(getattr(getattr(self.cfg.channels, name), "enabled", False)):
                return True
        return bool(self.cfg.channels.gmail.enabled and (self.cfg.channels.gmail.allow_send or self.cfg.channels.gmail.allow_compose))

    async def start(self) -> None:
        if self.webhook_worker is not None:
            self.webhook_worker.start()
        if self.outbound_dispatcher is not None:
            self.outbound_dispatcher.start()

    async def aclose(self) -> None:
        if self.webhook_worker is not None:
            await self.webhook_worker.stop()
        if self.outbound_dispatcher is not None:
            await self.outbound_dispatcher.stop()
        self.close()

    def close(self) -> None:
        for resource in (
            self.memory,
            self.governance,
            self.learning_loop,
            self.learning_experiments,
            self.model_router,
            self.model_cost_store,
            self.skills,
            self.plugins,
            self.webhook_worker,
            self.outbound_dispatcher,
            self.job_queue,
            self.outbound_messages,
            self.token_budget,
            self.run_store,
            getattr(self, "agent_run_idempotency", None),
            getattr(self, "side_effect_idempotency", None),
            self.approval_store,
            getattr(self, "dual_approval_store", None),
            getattr(self, "break_glass_store", None),
            self.webhook_security,
            getattr(self, "transactional_outbox", None),
        ):
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    def __del__(self) -> None:  # pragma: no cover - best-effort resource cleanup
        try:
            self.close()
        except Exception:
            pass

    def status(self) -> dict:
        model_router_status = self.model_router.status()
        resource_guard_backend = str(getattr(self.cfg.api_resource_guard, "backend", "memory"))
        cost_ledger_backend = model_router_status.get("cost_ledger_backend")
        side_effect_stats = {}
        stats = getattr(getattr(self, "side_effect_idempotency", None), "stats", None)
        if callable(stats):
            side_effect_stats = stats()
        return {
            "workspace": str(self.cfg.workspace.root),
            "tools": self.tools.names(),
            "skills": sorted(self.skills.skills),
            "plugins": sorted(getattr(self.plugins, "loaded", {})),
            "channels": sorted(self.adapters),
            "audit_log": str(self.cfg.permissions.audit_log),
            "storage": {"backend": self.storage_plan.backend, "multi_instance_safe": self.storage_plan.multi_instance_safe},
            "resource_guard": {
                "enabled": bool(self.cfg.api_resource_guard.enabled),
                "backend": resource_guard_backend,
                "multi_instance_safe": resource_guard_backend == "postgres",
            },
            "model_router": model_router_status,
            "cost_ledger": {
                "backend": cost_ledger_backend,
                "persistent": cost_ledger_backend is not None,
                "multi_instance_safe": cost_ledger_backend == "PostgresModelCostStore",
            },
            "idempotency": {
                "agent_run_enabled": getattr(self, "agent_run_idempotency", None) is not None,
                "side_effect_enabled": getattr(self, "side_effect_idempotency", None) is not None,
                "side_effect": side_effect_stats,
            },
            "release_evidence": {
                "summary_path": "release/real-ga-evidence-summary-1.12.6.json",
                "audit_path": "release/real-ga-evidence-audit-1.12.6.json",
                "status": "requires_external_real_ga_evidence",
            },
            "security": {"dual_approval_for_risks": list(self.cfg.permissions.require_dual_approval_for_risks), "break_glass_enabled": bool(self.cfg.permissions.break_glass_enabled)},
            "learning_enabled": self.cfg.learning.enabled,
            "jobs": self.job_queue.stats(),
            "outbound_messages": self.outbound_messages.stats(),
        }
