from __future__ import annotations

from typing import Literal, Optional, Union

import asyncio
import shlex
from pathlib import Path

from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE, SandboxConfig
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient
from omnidesk_agent.self_upgrade.models import TestResult


class SandboxRunner:
    """Upgrade test runner with argv allowlist and Docker/remote isolation.

    Production self-upgrade checks must use the same sandbox backend as the
    runtime, not silently fall back to argv on the host process.
    """

    DEFAULT_ALLOWED = [
        ["python", "-m", "compileall"],
        ["python3", "-m", "compileall"],
        ["pytest"],
        ["ruff", "check"],
        ["git", "diff"],
        ["git", "status"],
    ]

    def __init__(
        self,
        repo_root: Path,
        allowed_prefixes: Optional[list[list[str]]] = None,
        *,
        backend: Literal["argv", "docker", "remote_docker"] | None = None,
        docker_image: str | None = None,
        sandbox_cfg: SandboxConfig | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.allowed_prefixes = allowed_prefixes or self.DEFAULT_ALLOWED
        self.sandbox_cfg = sandbox_cfg or SandboxConfig(backend=backend or "argv", docker_image=docker_image or DEFAULT_SANDBOX_IMAGE)
        self.backend = str(backend or self.sandbox_cfg.backend)
        if self.backend not in {"argv", "docker", "remote_docker"}:
            raise ValueError(f"unsupported sandbox backend: {self.backend}")
        self.docker_image = docker_image or self.sandbox_cfg.docker_image

    def allowed(self, argv: list[str]) -> bool:
        return any(len(argv) >= len(prefix) and argv[:len(prefix)] == prefix for prefix in self.allowed_prefixes)

    async def run(self, command: Union[str, list], timeout: int = 120) -> TestResult:
        argv = [str(x) for x in command] if isinstance(command, list) else shlex.split(command)
        if not argv:
            return TestResult(False, str(command), "empty command", 2)
        if not self.allowed(argv):
            return TestResult(False, " ".join(argv), f"blocked by sandbox allowlist: {argv}", 126)

        if self.backend == "remote_docker":
            if not self.sandbox_cfg.runner_url:
                return TestResult(False, " ".join(argv), "remote sandbox runner_url is not configured", 2)
            client = RemoteSandboxClient(
                self.sandbox_cfg.runner_url,
                token_env=self.sandbox_cfg.runner_token_env,
                hmac_secret_env=getattr(self.sandbox_cfg, "runner_hmac_secret_env", "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"),
            )
            try:
                result = await client.run_command(argv=argv, workspace=self.repo_root, timeout_seconds=timeout, readonly=True)
            except Exception as exc:
                return TestResult(False, "remote_docker " + " ".join(argv), str(exc), 2)
            output = (result.stdout + ("\n" + result.stderr if result.stderr else ""))[-12000:]
            return TestResult(result.ok, "remote_docker " + " ".join(argv), output, int(result.exit_code))

        exec_argv = self._docker_command(argv) if self.backend == "docker" else argv
        proc = await asyncio.create_subprocess_exec(
            *exec_argv,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return TestResult(False, " ".join(exec_argv), f"Timed out after {timeout}s", 124)
        text = output.decode(errors="replace")[-12000:]
        return TestResult(proc.returncode == 0, " ".join(exec_argv), text, int(proc.returncode or 0))

    def _docker_command(self, argv: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm", "--network", "none", "--init",
            "--pull", str(getattr(self.sandbox_cfg, "pull_policy", "never")),
            "--log-driver", str(getattr(self.sandbox_cfg, "log_driver", "none")),
            "--oom-kill-disable=false",
            "--memory", str(getattr(self.sandbox_cfg, "memory_limit", "512m")),
            "--cpus", str(getattr(self.sandbox_cfg, "cpus", "1.0")),
            "--pids-limit", str(getattr(self.sandbox_cfg, "pids_limit", 128)),
            "--user", str(getattr(self.sandbox_cfg, "user", "65534:65534")),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", str(getattr(self.sandbox_cfg, "tmpfs", "/tmp:rw,noexec,nosuid,size=128m")),  # nosec B108
            "--mount", f"type=bind,src={self.repo_root},dst=/workspace,readonly",
            "-w", "/workspace",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            str(self.docker_image),
            *argv,
        ]
