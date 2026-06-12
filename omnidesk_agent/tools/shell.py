from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec


class ShellTool:
    name = "shell"

    DEFAULT_ALLOWED_PREFIXES = [
        ["python3", "-m", "compileall"],
        ["python", "-m", "compileall"],
        ["pytest"],
        ["ruff", "check"],
        ["git", "status"],
        ["git", "diff"],
        ["git", "branch"],
        ["git", "log"],
        ["git", "ls-tree"],
    ]

    def __init__(self, cwd: Path, cfg: PermissionConfig, sandbox_cfg: Optional[SandboxConfig] = None):
        self.cwd = cwd.expanduser().resolve()
        self.cfg = cfg
        self.sandbox_cfg = sandbox_cfg
        self.backend = getattr(sandbox_cfg, "backend", getattr(cfg, 'shell_backend', 'argv'))
        self.allowed_prefixes = list(getattr(cfg, "shell_allowed_commands", None) or self.DEFAULT_ALLOWED_PREFIXES)
        if getattr(cfg, "shell_upgrade_enabled", False):
            self.allowed_prefixes.extend([
                ["git", "add"], ["git", "commit"], ["git", "pull"], ["git", "push"],
                ["pip", "install", "-e"], ["python3", "-m", "pip", "install", "-e"],
            ])

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="Run allowlisted shell commands with argv execution. Does not use shell=True.",
            permissions=["shell.run"],
            actions={
                "run": ActionSpec("run", "Run an allowlisted command", {"command": "string | argv:list[string]"}, risk="critical", side_effect=True, requires_approval=True)
            },
        )

    def _argv(self, args: dict[str, Any]) -> list[str]:
        if "argv" in args:
            argv = [str(x) for x in args["argv"]]
        else:
            argv = shlex.split(str(args.get("command", "")))
        if not argv:
            raise ValueError("shell.run requires command or argv")
        return argv

    def _allowed(self, argv: list[str]) -> bool:
        for prefix in self.allowed_prefixes:
            if len(argv) >= len(prefix) and argv[:len(prefix)] == prefix:
                return True
        return False

    def _docker_argv(self, argv: list[str]) -> list[str]:
        image = getattr(self.sandbox_cfg, "docker_image", getattr(self.cfg, "shell_docker_image", "python:3.11-slim"))
        network = getattr(self.sandbox_cfg, "docker_network", getattr(self.cfg, "shell_docker_network", "none"))
        memory = getattr(self.sandbox_cfg, "memory_limit", getattr(self.cfg, "shell_docker_memory", "512m"))
        cpus = getattr(self.sandbox_cfg, "cpus", getattr(self.cfg, "shell_docker_cpus", "1.0"))
        return [
            "docker", "run", "--rm",
            "--network", str(network),
            "--memory", str(memory),
            "--cpus", str(cpus),
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-v", f"{self.cwd}:/workspace:rw",
            "-w", "/workspace",
            str(image),
            *argv,
        ]

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "run":
            raise ValueError(f"Unsupported shell action: {action}")

        argv = self._argv(args)
        expected = str(args.get("expected_result") or f"Run {' '.join(argv)}")
        if not self._allowed(argv):
            return ToolResult(False, error=f"Command not in allowlist: {argv}", summary="shell command blocked by allowlist")

        ctx.permissions.verify(proposal(
            "shell", "run",
            {"argv": argv, "expected_result": expected},
            "critical", "执行 allowlisted shell 命令", ctx,
        ))

        timeout = int(args.get("timeout", getattr(self.sandbox_cfg, "timeout_seconds", getattr(self.cfg, "max_shell_seconds", 30))))
        exec_argv = self._docker_argv(argv) if self.backend == 'docker' else argv
        proc = await asyncio.create_subprocess_exec(
            *exec_argv,
            cwd=str(self.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(False, error=f"Command timed out after {timeout}s", summary="shell timeout")

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        data = {
            "argv": argv,
            "exec_argv": exec_argv,
            "backend": self.backend,
            "exit_code": proc.returncode,
            "stdout": stdout[:8000],
            "stderr": stderr[:8000],
            "stdout_truncated": len(stdout) > 8000,
            "stderr_truncated": len(stderr) > 8000,
        }
        ok = proc.returncode == 0
        return ToolResult(ok, data=data, summary=f"shell exit {proc.returncode}", error=None if ok else stderr[:2000])
