from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE, PermissionConfig, SandboxConfig
from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.tools.base import ToolContext, proposal
from omnidesk_agent.tools.spec import ActionSpec, ToolSpec
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient
from omnidesk_agent.security.command_policy import (
    SAFE_CI_ALLOWED_PREFIXES,
    UPGRADE_ALLOWED_PREFIXES,
    argv_allowed,
    readonly_command,
)


class ShellTool:
    name = "shell"

    DEFAULT_ALLOWED_PREFIXES = SAFE_CI_ALLOWED_PREFIXES

    def __init__(self, cwd: Path, cfg: PermissionConfig, sandbox_cfg: Optional[SandboxConfig] = None):
        self.cwd = cwd.expanduser().resolve()
        self.cfg = cfg
        self.sandbox_cfg = sandbox_cfg
        self.backend = getattr(sandbox_cfg, "backend", getattr(cfg, 'shell_backend', 'argv'))
        self.allowed_prefixes = list(getattr(cfg, "shell_allowed_commands", None) or self.DEFAULT_ALLOWED_PREFIXES)
        if getattr(cfg, "shell_upgrade_enabled", False):
            self.allowed_prefixes.extend(UPGRADE_ALLOWED_PREFIXES)

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
        return argv_allowed(argv, self.allowed_prefixes)

    def _is_readonly_command(self, argv: list[str]) -> bool:
        return readonly_command(argv)

    def _is_production_runtime(self) -> bool:
        mode = (os.environ.get("OMNIDESK_ENV") or os.environ.get("APP_ENV") or os.environ.get("ENV") or "").strip().lower()
        return mode in {"prod", "production", "live"}

    def _runtime_backend_allowed(self, argv: list[str]) -> tuple[bool, str | None]:
        if not self._is_production_runtime():
            return True, None
        if self.backend == "argv":
            return False, "shell argv backend is forbidden in production runtime; use docker or remote_docker"
        if argv[:1] == ["pytest"] and self.backend not in {"docker", "remote_docker"}:
            return False, "pytest must run inside a sandbox backend in production runtime"
        return True, None

    def _docker_argv(self, argv: list[str]) -> list[str]:
        image = getattr(self.sandbox_cfg, "docker_image", getattr(self.cfg, "shell_docker_image", DEFAULT_SANDBOX_IMAGE))
        network = getattr(self.sandbox_cfg, "docker_network", getattr(self.cfg, "shell_docker_network", "none"))
        memory = getattr(self.sandbox_cfg, "memory_limit", getattr(self.cfg, "shell_docker_memory", "512m"))
        cpus = getattr(self.sandbox_cfg, "cpus", getattr(self.cfg, "shell_docker_cpus", "1.0"))
        docker_args = [
            "docker", "run", "--rm",
        ]
        if bool(getattr(self.sandbox_cfg, "init", True)):
            docker_args.append("--init")
        docker_args.extend([
            "--pull", str(getattr(self.sandbox_cfg, "pull_policy", "never")),
            "--log-driver", str(getattr(self.sandbox_cfg, "log_driver", "none")),
            "--oom-kill-disable=false",
            "--network", str(network),
            "--memory", str(memory),
            "--cpus", str(cpus),
            "--pids-limit", str(getattr(self.sandbox_cfg, "pids_limit", 128)),
            "--user", str(getattr(self.sandbox_cfg, "user", "65534:65534")),
            "--read-only",
            "--tmpfs", str(getattr(self.sandbox_cfg, "tmpfs", "/tmp:rw,noexec,nosuid,size=64m")),  # nosec B108
        ])
        for cap in getattr(self.sandbox_cfg, "cap_drop", ["ALL"]):
            docker_args.extend(["--cap-drop", str(cap)])
        for opt in getattr(self.sandbox_cfg, "security_opt", ["no-new-privileges"]):
            docker_args.extend(["--security-opt", str(opt)])
        mount_mode = "ro" if self._is_readonly_command(argv) else "rw"
        docker_args.extend([
            "--mount", f"type=bind,src={self.cwd},dst=/workspace,{'readonly' if mount_mode == 'ro' else 'rw'}",
            "-w", "/workspace",
            str(image),
            *argv,
        ])
        return docker_args

    async def call(self, action: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if action != "run":
            raise ValueError(f"Unsupported shell action: {action}")

        argv = self._argv(args)
        expected = str(args.get("expected_result") or f"Run {' '.join(argv)}")
        if not self._allowed(argv):
            return ToolResult(False, error=f"Command not in allowlist: {argv}", summary="shell command blocked by allowlist")
        allowed_backend, backend_error = self._runtime_backend_allowed(argv)
        if not allowed_backend:
            return ToolResult(False, error=backend_error, summary="shell backend blocked by production runtime policy")

        ctx.permissions.verify(proposal(
            "shell", "run",
            {"argv": argv, "expected_result": expected},
            "critical", "执行 allowlisted shell 命令", ctx,
        ))

        timeout = int(args.get("timeout", getattr(self.sandbox_cfg, "timeout_seconds", getattr(self.cfg, "max_shell_seconds", 30))))
        if self.backend == "remote_docker":
            if self.sandbox_cfg is None or not self.sandbox_cfg.runner_url:
                return ToolResult(False, error="remote sandbox runner_url is not configured", summary="shell remote sandbox not configured")
            client = RemoteSandboxClient(
                self.sandbox_cfg.runner_url,
                token_env=self.sandbox_cfg.runner_token_env,
                hmac_secret_env=getattr(self.sandbox_cfg, "runner_hmac_secret_env", "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"),
            )
            try:
                remote = await client.run_command(
                    argv=argv,
                    workspace=self.cwd,
                    timeout_seconds=timeout,
                    readonly=self._is_readonly_command(argv),
                )
            except Exception as exc:
                return ToolResult(False, error=str(exc), summary="shell remote sandbox failed")
            stdout = remote.stdout
            stderr = remote.stderr
            data = {
                "argv": argv,
                "exec_argv": ["remote_docker", *(argv or [])],
                "backend": self.backend,
                "exit_code": remote.exit_code,
                "stdout": stdout[:8000],
                "stderr": stderr[:8000],
                "stdout_truncated": len(stdout) > 8000,
                "stderr_truncated": len(stderr) > 8000,
            }
            return ToolResult(remote.ok, data=data, summary=f"shell remote exit {remote.exit_code}", error=None if remote.ok else stderr[:2000])

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
