from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from omnidesk_agent.models.base import ModelDelta, ModelRequest, ModelResponse
from omnidesk_agent.models.provider_errors import ChatGPTPlanError

if TYPE_CHECKING:
    from omnidesk_agent.models.providers import ProviderSettings


# Compatibility is intentionally pinned; real subscription verification remains a separate release gate.
SUPPORTED_CLI_VERSION = "2.1.302"
TIMEOUT_SECONDS = 120
CLEANUP_TIMEOUT_SECONDS = 2
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
# Fixed, non-private text: the real system/user content travels over stdin, never argv.
PUBLIC_SYSTEM_PROMPT = (
    "You are a text-only assistant inside a governed gateway. No tools are available. "
    "Answer the request in the message directly; never ask to run commands or read files."
)
_ALLOWED_BLOCKS = {"text", "thinking", "redacted_thinking"}


def _child_env() -> dict[str, str]:
    # The official CLI owns its saved login. API keys, OAuth tokens and Gateway secrets never enter its
    # environment, so a request can neither bill an API account nor use a token OmniDesk handled.
    allowed = {
        "HOME", "CLAUDE_CONFIG_DIR", "PATH", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME",
        "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    env = {name: value for name, value in os.environ.items() if name in allowed}
    env.update(DISABLE_TELEMETRY="1", DISABLE_AUTOUPDATER="1", DISABLE_ERROR_REPORTING="1",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
               CLAUDE_CODE_MAX_RETRIES="0", CLAUDE_CODE_RETRY_WATCHDOG="0",
               CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK="1", CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES="0")
    return env


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned invalid token usage; request stopped")
    usage: dict[str, int] = {}
    for field, source in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                          ("cached_input_tokens", "cache_read_input_tokens"), ("cache_creation_input_tokens", "cache_creation_input_tokens")):
        if source in raw:
            value = raw[source]
            if type(value) is not int or value < 0:
                raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned invalid token usage; request stopped")
            usage[field] = value
    if "input_tokens" not in usage or "output_tokens" not in usage:
        raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned invalid token usage; request stopped")
    usage["input_tokens"] += usage.get("cached_input_tokens", 0) + usage.pop("cache_creation_input_tokens", 0)
    return usage


def _parse_events(raw: bytes) -> tuple[str, dict[str, int], str]:
    """Accept exactly: one tool-less init, optional text-only assistant events, one successful result."""
    session_id = None
    model = None
    assistant_text = []
    result = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned invalid JSON events; request stopped") from None
        kind = event.get("type") if isinstance(event, dict) else None
        if not isinstance(kind, str) or result is not None:
            raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned an invalid or trailing event; request stopped")
        if kind == "system":
            sid = event.get("session_id")
            if (session_id is not None or event.get("subtype") != "init" or not isinstance(sid, str) or not sid.strip()
                    or event.get("tools") != [] or event.get("mcp_servers") != []
                    or not isinstance(event.get("model"), str) or not event["model"].strip()):
                raise ChatGPTPlanError("subscription_tools", "Claude Code CLI exposed tools or MCP servers; request stopped")
            session_id, model = sid, event["model"]
        elif session_id is None:
            raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI emitted events before initialization; request stopped")
        elif kind == "assistant":
            message = event.get("message")
            if (event.get("session_id") != session_id or not isinstance(message, dict)
                    or message.get("role") != "assistant" or message.get("model") != model):
                raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned a mismatched assistant event; request stopped")
            blocks = message.get("content")
            if not isinstance(blocks, list) or event.get("is_api_error_message") or event.get("error"):
                raise ChatGPTPlanError("subscription_response", "Claude Code CLI request failed; request stopped")
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") not in _ALLOWED_BLOCKS:
                    raise ChatGPTPlanError("subscription_tools", "Claude Code CLI attempted a tool or unsupported block; request stopped")
                if block["type"] == "text":
                    if not isinstance(block.get("text"), str):
                        raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned invalid assistant text; request stopped")
                    assistant_text.append(block["text"])
        elif kind == "result":
            result = event
        else:
            raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned an unsupported event; request stopped")
    if result is None or session_id is None:
        raise ChatGPTPlanError("subscription_incomplete", "Claude Code CLI ended without a completed result; request stopped")
    # An authentication or API failure is reported as is_error=true with subtype "success"; check the flag.
    if result.get("is_error") is not False or result.get("subtype") != "success" or result.get("session_id") != session_id:
        raise ChatGPTPlanError("subscription_response", "Claude Code CLI request failed; request stopped")
    if result.get("permission_denials") not in (None, []):
        raise ChatGPTPlanError("subscription_tools", "Claude Code CLI attempted a tool; request stopped")
    text = result.get("result")
    if not isinstance(text, str) or not text.strip() or text != "".join(assistant_text):
        raise ChatGPTPlanError("subscription_protocol", "Claude Code CLI returned an invalid message; request stopped")
    return text, _usage(result.get("usage")), session_id


class ClaudeCodeCLIProvider:
    provider_name = "claude_code_cli"

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name
        if os.name != "posix":
            raise ChatGPTPlanError("subscription_platform", "Claude Code CLI provider requires POSIX process-group cleanup; this platform is unsupported")
        if settings.api_key_env or settings.base_url or settings.api_version or settings.region or settings.extra_headers or settings.extra_body:
            raise ValueError("Claude Code CLI uses its saved login and does not accept HTTP, API-key or extra settings")
        candidate = settings.claude_code_cli_path or shutil.which("claude")
        if not candidate or not Path(candidate).is_absolute():
            raise ValueError("Claude Code CLI requires an installed executable at an absolute trusted path")
        self.binary = str(Path(candidate).resolve())
        binary = Path(self.binary)
        if not binary.is_file() or not os.access(binary, os.X_OK) or binary.stat().st_mode & 0o022:
            raise ValueError("Claude Code CLI executable is missing, not executable, or writable by other users")

    async def _run(self, argv: list[str], *, env: dict[str, str], cwd: Path, input_data: bytes = b"",
                   system_prompt: bytes | None = None) -> tuple[bytes, bytes, int]:
        proc = None
        tasks: list[asyncio.Task] = []
        pending = None
        system_read = None
        system_pipe = None
        system_transport = None
        try:
            if system_prompt is not None:
                system_read, system_write = os.pipe()
                system_pipe = os.fdopen(system_write, "wb", buffering=0)
                argv = [*argv, "--system-prompt-file", f"/dev/fd/{system_read}"]
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=MAX_OUTPUT_BYTES + 1, pass_fds=() if system_read is None else (system_read,),
            )

            if system_read is not None:
                os.close(system_read)
                system_read = None

            async def write_system():
                nonlocal system_transport
                loop = asyncio.get_running_loop()
                protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
                system_transport, _ = await loop.connect_write_pipe(lambda: protocol, system_pipe)
                writer = asyncio.StreamWriter(system_transport, protocol, None, loop)
                try:
                    writer.write(system_prompt)
                    await writer.drain()
                finally:
                    writer.close()
                await writer.wait_closed()

            async def write_input():
                try:
                    proc.stdin.write(input_data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            async def read_output(pipe):
                data = bytearray()
                while chunk := await pipe.read(65536):
                    data.extend(chunk)
                    if len(data) > MAX_OUTPUT_BYTES:
                        raise ChatGPTPlanError("subscription_output_limit", "Claude Code CLI output exceeded its limit; request stopped")
                return bytes(data)

            tasks = [asyncio.create_task(c) for c in (write_input(), read_output(proc.stdout), read_output(proc.stderr), proc.wait())]
            if system_prompt is not None:
                tasks.append(asyncio.create_task(write_system()))
            pending = asyncio.gather(*tasks)
            result = await asyncio.wait_for(pending, timeout=TIMEOUT_SECONDS)
            return result[1], result[2], result[3]
        except asyncio.TimeoutError:
            raise ChatGPTPlanError("subscription_transport", "Claude Code CLI timed out; request stopped") from None
        except ValueError:
            raise ChatGPTPlanError("subscription_output_limit", "Claude Code CLI output exceeded its line limit; request stopped") from None
        except (OSError, BrokenPipeError, ConnectionResetError):
            raise ChatGPTPlanError("subscription_unavailable", "Claude Code CLI could not start or disconnected; verify the installed executable") from None
        finally:
            if system_read is not None:
                os.close(system_read)
            if system_transport is not None:
                system_transport.close()
            elif system_pipe is not None:
                system_pipe.close()
            if proc is not None:
                try:
                    # The leader can exit while a descendant still owns the output pipes.
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for task in tasks:
                if not task.done():
                    task.cancel()
            if proc is not None:
                async def cleanup():
                    await asyncio.gather(*tasks, *([pending] if pending is not None else []), return_exceptions=True)
                    await proc.communicate()
                try:
                    await asyncio.wait_for(cleanup(), timeout=CLEANUP_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    raise ChatGPTPlanError("subscription_transport", "Claude Code CLI process cleanup exceeded its limit; request stopped") from None

    def _print_argv(self) -> list[str]:
        # --tools "" removes every built-in tool; --strict-mcp-config with no --mcp-config removes MCP servers;
        # --disable-slash-commands drops skills; sessions are never written; no resume, fork or background flags.
        return [
            self.binary, "-p", "--output-format", "stream-json", "--verbose",
            "--model", self.model, "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--permission-mode", "dontAsk", "--safe-mode",
            "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
        ]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.images:
            raise ChatGPTPlanError("subscription_input", "Claude Code CLI provider currently accepts text inputs only")
        environment = _child_env()
        with TemporaryDirectory(prefix="omnidesk-claude-cli-") as directory:
            workdir = Path(directory).resolve()
            output, _, code = await self._run([self.binary, "--version"], env=environment, cwd=workdir)
            version = re.search(rb"(?m)^([^\s]+) \(Claude Code\)\s*$", output)
            if code or version is None or version[1].decode("ascii", errors="replace") != SUPPORTED_CLI_VERSION:
                raise ChatGPTPlanError("subscription_cli_version", f"Claude Code CLI {SUPPORTED_CLI_VERSION} is the pinned compatibility target; other versions require validation")
            output, _, code = await self._run([self.binary, "auth", "status", "--json"], env=environment, cwd=workdir)
            try:
                status = json.loads(output)
                if (code or status["loggedIn"] is not True or status["authMethod"] != "claude.ai"
                        or status.get("apiProvider") != "firstParty"):
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                raise ChatGPTPlanError("subscription_auth", "An existing official Claude Code subscription login is required") from None
            system = PUBLIC_SYSTEM_PROMPT + "\n" + request.system
            if request.json_mode:
                system += "\nReturn only valid JSON, with no markdown."
            output, _, code = await self._run(
                self._print_argv(), env=environment, cwd=workdir, input_data=request.user.encode("utf-8"),
                system_prompt=system.encode("utf-8"),
            )
            if code:
                raise ChatGPTPlanError("subscription_response", "Claude Code CLI request failed; verify login, model access and CLI compatibility")
            text, usage, session_id = _parse_events(output)
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=usage,
                             raw={"id": session_id, "accepted_tool_items": 0, "transport": "claude_code_cli"})

    async def stream(self, request: ModelRequest):
        response = await self.complete(request)
        yield ModelDelta(sequence=1, provider=self.provider_name, model=self.model, profile=self.profile_name,
                         text=response.text, usage=response.usage, finish_reason="stop",
                         provider_request_id=(response.raw or {}).get("id"), native=False)
