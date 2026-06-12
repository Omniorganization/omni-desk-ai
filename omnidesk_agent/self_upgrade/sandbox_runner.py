from __future__ import annotations
from typing import Optional, Union

import asyncio
import shlex
from pathlib import Path

from omnidesk_agent.self_upgrade.models import TestResult


class SandboxRunner:
    """Upgrade test runner with argv allowlist and optional Docker isolation.

    Docker mode runs with no network, read-only source mount, constrained CPU/memory,
    and a tmpfs writable area. The runner never uses shell=True.
    """

    DEFAULT_ALLOWED = [
        ["python", "-m", "compileall"],
        ["python3", "-m", "compileall"],
        ["pytest"],
        ["ruff", "check"],
        ["git", "diff"],
        ["git", "status"],
    ]

    def __init__(self, repo_root: Path, allowed_prefixes: Optional[list[list[str]]] = None, *, backend: str = "argv", docker_image: str = "python:3.11-slim"):
        self.repo_root = repo_root.resolve()
        self.allowed_prefixes = allowed_prefixes or self.DEFAULT_ALLOWED
        if backend not in {"argv", "docker"}:
            raise ValueError(f"unsupported sandbox backend: {backend}")
        self.backend = backend
        self.docker_image = docker_image

    def allowed(self, argv: list[str]) -> bool:
        return any(len(argv) >= len(prefix) and argv[:len(prefix)] == prefix for prefix in self.allowed_prefixes)

    async def run(self, command: Union[str, list], timeout: int = 120) -> TestResult:
        argv = [str(x) for x in command] if isinstance(command, list) else shlex.split(command)
        if not argv:
            return TestResult(False, str(command), "empty command", 2)
        if not self.allowed(argv):
            return TestResult(False, " ".join(argv), f"blocked by sandbox allowlist: {argv}", 126)

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
            return TestResult(False, " ".join(exec_argv), f"Timed out after {timeout}s", 124)
        text = output.decode(errors="replace")[-12000:]
        return TestResult(proc.returncode == 0, " ".join(exec_argv), text, int(proc.returncode or 0))


    def _docker_command(self, argv: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--memory", "512m", "--cpus", "1.0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "-v", f"{self.repo_root}:/workspace:ro",
            "-w", "/workspace",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            self.docker_image,
            *argv,
        ]
