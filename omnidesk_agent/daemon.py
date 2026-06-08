from __future__ import annotations

from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.llm import RuleBasedLLM, OpenAIChatLLM
from omnidesk_agent.core.orchestrator import Orchestrator
from omnidesk_agent.core.planner import HierarchicalPlanner
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.skills.registry import SkillRegistry
from omnidesk_agent.tools.computer import ComputerTool
from omnidesk_agent.tools.files import FilesTool
from omnidesk_agent.tools.registry import ToolRegistry
from omnidesk_agent.tools.shell import ShellTool


class OmniDeskRuntime:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.permissions = PermissionManager(cfg.permissions)
        self.memory = ExperienceStore(cfg.workspace.memory_db)
        self.skills = SkillRegistry(cfg.workspace.skills_dirs)
        self.tools = ToolRegistry()
        self._register_builtin_tools()
        self.skills.load()
        llm = RuleBasedLLM() if cfg.llm.provider == "rule" else OpenAIChatLLM(cfg.llm)
        self.planner = HierarchicalPlanner(llm=llm, memory=self.memory, skills=self.skills, tools=self.tools)
        self.orchestrator = Orchestrator(self.planner, self.tools, self.permissions, self.memory)

    def _register_builtin_tools(self) -> None:
        self.tools.register(ComputerTool())
        self.tools.register(ShellTool(self.cfg.workspace.root, self.cfg.permissions))
        self.tools.register(FilesTool(self.cfg.workspace.root))

    def status(self) -> dict:
        return {
            "workspace": str(self.cfg.workspace.root),
            "tools": self.tools.names(),
            "skills": sorted(self.skills.skills),
            "audit_log": str(self.cfg.permissions.audit_log),
        }
