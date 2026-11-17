from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from omnidesk_agent.tools.base import json_sha256, permission_guarded



ALLOWED_PLUGIN_PERMISSIONS = {
    "files.read", "files.write",
    "browser.read", "browser.write",
    "network.read", "network.write",
    "gmail.read", "gmail.compose",
    "skills.read",
    "memory.read", "memory.write",
    "plugin.call",
}


def validate_plugin_permissions(permissions: list[str]) -> None:
    invalid = [p for p in permissions if p not in ALLOWED_PLUGIN_PERMISSIONS]
    if invalid:
        raise PermissionError(f"unsupported plugin permissions: {invalid}")


class SubprocessPluginTool:
    """Run plugin entrypoints in a child Python process via JSON stdin/stdout.

    The plugin entrypoint may expose:
      def call(action: str, args: dict) -> dict

    It must not import the main runtime directly.
    """

    def __init__(
        self,
        name: str,
        entrypoint: Path,
        permissions: list[str],
        timeout_seconds: int = 30,
        max_output_bytes: int = 200000,
        *,
        prepare_files: Optional[Callable[[Path], Path]] = None,
    ):
        self.name = name
        self.entrypoint = entrypoint
        validate_plugin_permissions(permissions)
        self.permissions = permissions
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        # Set by PluginRegistry: copies the plugin into the given directory,
        # verifies the copy against the signature and returns the entrypoint
        # inside it. Every call runs from such a fresh copy, so files changed
        # after loading (or while a call starts) never run.
        self.prepare_files = prepare_files

    @contextlib.asynccontextmanager
    async def _verified_entrypoint(self) -> AsyncIterator[Path]:
        if self.prepare_files is None:
            yield self.entrypoint
            return
        with tempfile.TemporaryDirectory(prefix="omnidesk-plugin-", ignore_cleanup_errors=True) as tmp:
            # Copying and hashing every file is blocking work.
            yield await asyncio.to_thread(self.prepare_files, Path(tmp))

    def _approve_call(self, args: dict[str, Any], ctx: Any, reason: str, **extra: Any) -> dict[str, Any]:
        """Ask approval for this exact call and return the payload it approved.

        The approver sees, and the scope hash binds, the plugin arguments.
        """
        from omnidesk_agent.tools.base import proposal

        plugin_action = args.get("plugin_action")
        plugin_args = args.get("plugin_args", {})
        approved = {
            "plugin_action": plugin_action,
            "plugin_args": plugin_args,
            "plugin_args_sha256": json_sha256(plugin_args),
            "permissions": self.permissions,
            **extra,
        }
        ctx.permissions.verify(proposal(self.name, "call", approved, "high", reason, ctx))
        return {"action": plugin_action, "args": plugin_args, "granted_permissions": self.permissions}

    def spec(self):
        from omnidesk_agent.tools.spec import ActionSpec, ToolSpec, obj_schema
        return ToolSpec(
            name=self.name,
            description=f"Subprocess plugin {self.name}",
            permissions=self.permissions,
            actions={
                "call": ActionSpec(
                    "call",
                    "Call subprocess plugin action",
                    obj_schema({
                        "plugin_action": {"type": "string"},
                        "plugin_args": {"type": "object"},
                    }, required=["plugin_action"], additional=False),
                    risk="high",
                    side_effect=True,
                    requires_approval=True,
                )
            },
        )

    @permission_guarded
    async def call(self, action: str, args: dict[str, Any], ctx) -> Any:
        from omnidesk_agent.core.models import ToolResult

        if action != "call":
            raise ValueError("subprocess plugin tool only supports action=call")
        payload = self._approve_call(args, ctx, "调用隔离子进程插件")
        async with self._verified_entrypoint() as entrypoint:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "-I",
                # -B: never write __pycache__ next to the plugin source, which
                # the signature checks refuse.
                "-B",
                str(entrypoint),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(entrypoint.parent),
                env={"PYTHONNOUSERSITE": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("utf-8")), timeout=self.timeout_seconds)
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            return ToolResult(False, error="plugin output exceeded limit", summary=f"plugin {self.name} output limit exceeded")
        if proc.returncode != 0:
            return ToolResult(False, error=stderr.decode("utf-8", errors="replace")[:4000], summary=f"plugin {self.name} failed")
        try:
            data = json.loads(stdout.decode("utf-8"))
        except Exception:
            data = {"stdout": stdout.decode("utf-8", errors="replace")}
        return ToolResult(True, data=data, summary=f"plugin {self.name} completed")
