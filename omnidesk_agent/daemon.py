from __future__ import annotations
from pathlib import Path
from omnidesk_agent.channels.dingtalk import DingTalkChannel
from omnidesk_agent.channels.gmail import GmailChannel
from omnidesk_agent.channels.lark_feishu import FeishuChannel, LarkChannel
from omnidesk_agent.channels.line import LineChannel
from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.channels.wechat_official import WeChatOfficialChannel
from omnidesk_agent.channels.x_channel import XChannel
from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.execution_strategy import ResultOrientedExecutionStrategy
from omnidesk_agent.core.llm import RuleBasedLLM, RouterLLMAdapter
from omnidesk_agent.models.router import build_model_router
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.run_store import RunStore
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.core.structured_planner import LLMStructuredPlanner
from omnidesk_agent.core.token_budget import TokenBudgetConfig, TokenBudgetManager
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.plugins.registry import PluginRegistry
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.webhook_security import WebhookSecurity
from omnidesk_agent.skills.registry import SkillRegistry
from omnidesk_agent.tools.browser import BrowserTool
from omnidesk_agent.tools.channel_send import ChannelSendTool
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.git_tool import GitTool
from omnidesk_agent.tools.gmail_tool import GmailTool
from omnidesk_agent.tools.vision import VisionGroundingTool
from omnidesk_agent.tools.pr_tool import PullRequestTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.shell import ShellTool
from omnidesk_agent.tools.test_tool import TestTool
from omnidesk_agent.tools.ui_bridge_tool import UIBridgeTool

class OmniDeskRuntime:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.approval_store = ApprovalStore(cfg.workspace.root / 'approvals.sqlite3', ttl_seconds=cfg.permissions.approval_ttl_seconds)
        self.permissions = PermissionManager(cfg.permissions, self.approval_store)
        self.webhook_security = WebhookSecurity(cfg.workspace.root / 'webhooks.sqlite3')
        self.memory = ExperienceStore(cfg.workspace.memory_db)
        self.token_budget = TokenBudgetManager(cfg.workspace.root / "token_budget.sqlite3", TokenBudgetConfig(max_input_chars=cfg.llm.max_input_chars, max_output_tokens=cfg.llm.max_output_tokens, per_task_max_llm_calls=cfg.llm.per_task_max_llm_calls, cache_ttl_seconds=cfg.llm.cache_ttl_seconds, enable_cache=cfg.llm.enable_cache, require_approval_above_estimated_tokens=cfg.llm.require_approval_above_estimated_tokens))
        self.execution_strategy = ResultOrientedExecutionStrategy()
        self.run_store = RunStore(cfg.workspace.root / 'runs.sqlite3')
        self.skills = SkillRegistry(cfg.workspace.skills_dirs)
        self.plugins = PluginRegistry(cfg.workspace.plugins_dirs, cfg.plugins)
        self.tools = ToolRegistry()
        self.adapters = self._build_channel_adapters()
        self.model_router = build_model_router(cfg.models, self.token_budget)
        self._register_builtin_tools()
        self.skills.load()
        self.plugins.load_into(self.tools, cfg)
        llm = RuleBasedLLM() if cfg.llm.provider == "rule" else RouterLLMAdapter(self.model_router, task="planner")
        self.rule_planner = HierarchicalPlanner(llm=llm, memory=self.memory, skills=self.skills, tools=self.tools)
        self.planner = self.rule_planner if cfg.llm.provider == 'rule' else LLMStructuredPlanner(self.model_router, self.memory, self.skills, self.tools, self.rule_planner)
        self.orchestrator = Orchestrator(self.planner, self.tools, self.permissions, self.memory, self.execution_strategy, self.run_store, self.approval_store)

    def _build_channel_adapters(self) -> dict:
        return {"telegram": TelegramChannel(self.cfg.channels.telegram), "whatsapp_cloud": WhatsAppCloudChannel(self.cfg.channels.whatsapp_cloud), "wechat_official": WeChatOfficialChannel(self.cfg.channels.wechat_official), "meta_graph": MetaGraphChannel(self.cfg.channels.meta_graph), "dingtalk": DingTalkChannel(self.cfg.channels.dingtalk), "lark": LarkChannel(self.cfg.channels.lark), "feishu": FeishuChannel(self.cfg.channels.feishu), "line": LineChannel(self.cfg.channels.line), "x": XChannel(self.cfg.channels.x), "gmail": GmailChannel(self.cfg.channels.gmail)}

    def _register_builtin_tools(self) -> None:
        self.tools.register(ComputerTool(self.cfg.workspace.root / 'screenshots'))
        self.tools.register(ShellTool(self.cfg.workspace.root, self.cfg.permissions))
        self.tools.register(FilesTool(self.cfg.workspace.root))
        self.tools.register(GitTool(Path.cwd()))
        self.tools.register(TestTool(Path.cwd()))
        self.tools.register(ChannelSendTool(self.adapters))
        self.tools.register(UIBridgeTool(self.cfg.channels.ui_bridge, self.tools))
        self.tools.register(BrowserTool(self.cfg.channels.chrome))
        self.tools.register(GmailTool(self.adapters["gmail"]))
        self.tools.register(VisionGroundingTool(self.model_router))
        self.tools.register(PullRequestTool(Path.cwd()))

    def status(self) -> dict:
        return {"workspace": str(self.cfg.workspace.root), "tools": self.tools.names(), "skills": sorted(self.skills.skills), "plugins": sorted(getattr(self.plugins, "loaded", {})), "channels": sorted(self.adapters), "audit_log": str(self.cfg.permissions.audit_log)}
