from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from omnidesk_agent.core.models import ToolResult
from omnidesk_agent.config import DEFAULT_SANDBOX_IMAGE
from omnidesk_agent.plugins.subprocess_runner import SubprocessPluginTool, validate_plugin_permissions
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient
from omnidesk_agent.tools.base import proposal


class DockerPluginTool(SubprocessPluginTool):
    """Run a plugin in Docker with no network and bounded resources.

    This is intended for production plugin execution. It keeps the same JSON
    stdin/stdout contract as SubprocessPluginTool, but runs in an isolated image.
    """

    def __init__(
        self,
        name: str,
        entrypoint: Path,
        permissions: list[str],
        timeout_seconds: int = 30,
        max_output_bytes: int = 200000,
        image: str = DEFAULT_SANDBOX_IMAGE,
    ):
        validate_plugin_permissions(permissions)
        super().__init__(name, entrypoint, permissions, timeout_seconds, max_output_bytes)
        self.image = image

    def docker_argv(self) -> list[str]:
        return [
            "docker", "run", "--rm",
            "--network", "none",
            "--init",
            "--pull", "never",
            "--log-driver", "none",
            "--oom-kill-disable=false",
            "--memory", "256m",
            "--cpus", "0.5",
            "--pids-limit", "128",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",  # nosec B108
            "--mount", f"type=bind,src={self.entrypoint.parent},dst=/plugin,readonly",
            "-w", "/plugin",
            self.image,
            "python3", "-I", self.entrypoint.name,
        ]

    async def call(self, action: str, args: dict[str, Any], ctx) -> ToolResult:
        if action != "call":
            raise ValueError("docker plugin tool only supports action=call")
        ctx.permissions.verify(proposal(
            self.name,
            "call",
            {"plugin_action": args.get("plugin_action"), "permissions": self.permissions, "sandbox": "docker"},
            "high",
            "调用 Docker 隔离插件",
            ctx,
        ))
        payload = json.dumps({
            "action": args.get("plugin_action"),
            "args": args.get("plugin_args", {}),
            "granted_permissions": self.permissions,
        }).encode("utf-8")
        proc = await asyncio.create_subprocess_exec(
            *self.docker_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="started")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="timeout")
            return ToolResult(False, error=f"plugin timed out after {self.timeout_seconds}s", summary=f"plugin {self.name} timeout")
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="output_limit")
            return ToolResult(False, error="plugin output exceeded limit", summary=f"plugin {self.name} output limit exceeded")
        if proc.returncode != 0:
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="error")
            return ToolResult(False, error=stderr.decode("utf-8", errors="replace")[:4000], summary=f"plugin {self.name} failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except Exception:
            data = {"stdout": stdout.decode("utf-8", errors="replace")}
        self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="ok")
        return ToolResult(True, data=data, summary=f"plugin {self.name} completed in docker")

    @staticmethod
    def _metric(ctx, name: str, **labels: Any) -> None:
        metrics = getattr(getattr(ctx, "permissions", None), "metrics", None)
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            inc(name, **labels)


class RemoteDockerPluginTool(DockerPluginTool):
    """Run a plugin through the remote Docker sandbox-runner service.

    This keeps the application container free of the Docker socket. The remote
    runner executes the same plugin stdin/stdout JSON contract in a digest-pinned,
    no-network container.
    """

    def __init__(
        self,
        name: str,
        entrypoint: Path,
        permissions: list[str],
        *,
        runner_url: str,
        runner_token_env: str = "OMNIDESK_SANDBOX_RUNNER_TOKEN",
        runner_hmac_secret_env: str = "OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET",
        timeout_seconds: int = 30,
        max_output_bytes: int = 200000,
        image: str = DEFAULT_SANDBOX_IMAGE,
    ):
        super().__init__(name, entrypoint, permissions, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes, image=image)
        self.runner_url = runner_url
        self.runner_token_env = runner_token_env
        self.runner_hmac_secret_env = runner_hmac_secret_env

    async def call(self, action: str, args: dict[str, Any], ctx) -> ToolResult:
        if action != "call":
            raise ValueError("remote docker plugin tool only supports action=call")
        ctx.permissions.verify(proposal(
            self.name,
            "call",
            {"plugin_action": args.get("plugin_action"), "permissions": self.permissions, "sandbox": "remote_docker"},
            "high",
            "调用远程 Docker 隔离插件",
            ctx,
        ))
        payload = json.dumps({
            "action": args.get("plugin_action"),
            "args": args.get("plugin_args", {}),
            "granted_permissions": self.permissions,
        }, ensure_ascii=False).encode("utf-8")
        client = RemoteSandboxClient(
            self.runner_url,
            token_env=self.runner_token_env,
            hmac_secret_env=self.runner_hmac_secret_env,
        )
        try:
            remote = await client.run_command(
                argv=["python3", "-I", self.entrypoint.name],
                workspace=self.entrypoint.parent,
                timeout_seconds=self.timeout_seconds,
                readonly=True,
                stdin=payload,
                purpose="plugin",
                image=self.image,
            )
        except Exception as exc:
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="remote_error")
            return ToolResult(False, error=str(exc), summary=f"plugin {self.name} remote sandbox failed")
        stdout = remote.stdout.encode("utf-8", errors="replace")
        stderr = remote.stderr.encode("utf-8", errors="replace")
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="output_limit")
            return ToolResult(False, error="plugin output exceeded limit", summary=f"plugin {self.name} output limit exceeded")
        if not remote.ok:
            self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="error")
            return ToolResult(False, error=remote.stderr[:4000], summary=f"plugin {self.name} failed in remote docker")
        try:
            data = json.loads(remote.stdout)
        except Exception:
            data = {"stdout": remote.stdout}
        self._metric(ctx, "omnidesk_plugin_call_total", plugin=self.name, status="ok")
        return ToolResult(True, data=data, summary=f"plugin {self.name} completed in remote docker")
