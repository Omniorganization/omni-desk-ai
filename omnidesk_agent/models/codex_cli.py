from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from omnidesk_agent.models.base import ModelDelta, ModelRequest, ModelResponse
from omnidesk_agent.models.provider_errors import ChatGPTPlanError

if TYPE_CHECKING:
    from omnidesk_agent.models.providers import ProviderSettings


SUPPORTED_CLI_VERSION = "0.162.0-alpha.17.2"
CLI_PROVIDER_ID = "omnidesk_codex_subscription"
TIMEOUT_SECONDS = 120
CLEANUP_TIMEOUT_SECONDS = 2
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAC_BUNDLED_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
DISABLED_FEATURES = (
    "shell_tool", "view_image", "code_mode_host", "code_mode", "code_mode_only", "apps", "plugins", "hooks",
    "multi_agent", "multi_agent_v2", "computer_use", "browser_use", "browser_use_external",
    "image_generation", "sleep_tool", "tool_suggest", "memories", "skill_search", "chronicle",
    "workspace_dependencies", "shell_snapshot", "current_time_reminder", "token_budget",
    "goals", "send_message_to_user_async", "agent_message_board", "unified_exec", "unified_exec_tty",
    "request_permissions_tool", "deferred_executor", "deferred_tool_world_state", "stable_environment_tools",
    "in_app_browser", "in_app_chat", "in_app_dictation", "in_app_local_automation", "in_app_updates",
    "in_app_voice", "realtime_conversation", "worktrees", "remote_plugin",
    "enable_mcp_apps", "codex_apps_mcp_2026_07_28", "multi_agent_v2_dynamic_tools",
    "standalone_web_search", "unbounded_connection_retries", "daemon_auto_start", "skill_mcp_dependency_install",
)


def _child_env() -> dict[str, str]:
    # The official CLI owns saved authentication; Gateway and app secrets never enter its environment.
    allowed = {
        "HOME", "CODEX_HOME", "PATH", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME",
        "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


def _event(line: bytes) -> dict[str, Any]:
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned invalid JSON events; request stopped") from None
    if not isinstance(event, dict):
        raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned invalid events; request stopped")
    kind = event.get("type")
    if not isinstance(kind, str):
        raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an invalid event type; request stopped")
    if kind in {"error", "turn.failed"}:
        raise ChatGPTPlanError("subscription_response", "Codex CLI request failed; request stopped")
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning"):
            raise ChatGPTPlanError("subscription_tools", "Codex CLI attempted a tool or unsupported item; request stopped")
    elif kind not in {"thread.started", "turn.started", "turn.completed"}:
        raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an unsupported event; request stopped")
    return event


def _parse_events(raw: bytes) -> tuple[str, dict[str, Any], str | None]:
    text = ""
    usage: dict[str, Any] = {}
    thread_id = None
    state = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = _event(line)
        if state == 4:
            raise ChatGPTPlanError("subscription_protocol", "Codex CLI emitted events after completion; request stopped")
        kind = event["type"]
        if kind == "thread.started":
            value = event.get("thread_id")
            if state != 0 or not isinstance(value, str) or not value.strip():
                raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an invalid thread start; request stopped")
            thread_id = value
            state = 1
        elif kind == "turn.started":
            if state != 1:
                raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an invalid turn start; request stopped")
            state = 2
        elif kind in {"item.started", "item.updated", "item.completed"}:
            if state != 2:
                raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an item outside its active turn; request stopped")
            if kind == "item.completed" and event["item"]["type"] == "agent_message":
                value = event["item"].get("text")
                if not isinstance(value, str) or not value.strip():
                    raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an invalid message; request stopped")
                text = value
                state = 3
        elif kind == "turn.completed":
            value = event.get("usage")
            if state != 3 or not isinstance(value, dict):
                raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned an invalid turn completion; request stopped")
            fields = ("input_tokens", "output_tokens") + (("cached_input_tokens",) if "cached_input_tokens" in value else ())
            for field in fields:
                if type(value.get(field)) is not int or value[field] < 0:
                    raise ChatGPTPlanError("subscription_protocol", "Codex CLI returned invalid token usage; request stopped")
            usage = {field: value[field] for field in ("input_tokens", "output_tokens", "cached_input_tokens") if field in value}
            state = 4
    if state != 4:
        raise ChatGPTPlanError("subscription_incomplete", "Codex CLI ended without a completed final message; request stopped")
    return text, usage, thread_id


class CodexCLIProvider:
    provider_name = "codex_cli"

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self.model = settings.model
        self.profile_name = settings.profile_name
        if os.name != "posix":
            raise ChatGPTPlanError("subscription_platform", "Codex CLI provider requires POSIX process-group cleanup; this platform is unsupported")
        if settings.api_key_env or settings.base_url or settings.api_version or settings.region or settings.extra_headers or settings.extra_body:
            raise ValueError("Codex CLI uses saved ChatGPT login and does not accept HTTP, API-key or extra settings")
        configured = settings.codex_cli_path
        candidate = configured or (str(MAC_BUNDLED_CLI) if MAC_BUNDLED_CLI.is_file() else shutil.which("codex"))
        if not candidate or not Path(candidate).is_absolute():
            raise ValueError("Codex CLI requires an installed executable at an absolute trusted path")
        self.binary = str(Path(candidate).resolve())
        binary = Path(self.binary)
        if not binary.is_file() or not os.access(binary, os.X_OK) or (os.name != "nt" and binary.stat().st_mode & 0o022):
            raise ValueError("Codex CLI executable is missing, not executable, or writable by other users")

    async def _run(self, argv: list[str], *, env: dict[str, str], cwd: Path, input_data: bytes = b"", check_events: bool = False):
        if os.name != "posix":
            raise ChatGPTPlanError("subscription_platform", "Codex CLI provider requires POSIX process-group cleanup; request stopped")
        proc = None
        tasks = []
        pending_run = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

            async def write_input():
                try:
                    proc.stdin.write(input_data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            async def read_output(pipe, events: bool):
                data = bytearray()
                pending = bytearray()
                while chunk := await pipe.read(65536):
                    data.extend(chunk)
                    if len(data) > MAX_OUTPUT_BYTES:
                        raise ChatGPTPlanError("subscription_output_limit", "Codex CLI output exceeded its limit; request stopped")
                    if events:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                _event(bytes(line))
                if events and pending.strip():
                    _event(bytes(pending))
                return bytes(data)

            tasks = [asyncio.create_task(coroutine) for coroutine in
                     (write_input(), read_output(proc.stdout, check_events), read_output(proc.stderr, False), proc.wait())]
            pending_run = asyncio.gather(*tasks)
            result = await asyncio.wait_for(pending_run, timeout=TIMEOUT_SECONDS)
            return result[1], result[2], result[3]
        except asyncio.TimeoutError:
            raise ChatGPTPlanError("subscription_transport", "Codex CLI timed out; request stopped") from None
        except OSError:
            raise ChatGPTPlanError("subscription_unavailable", "Codex CLI could not start; verify the installed executable") from None
        finally:
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
                    pending = [*tasks, *([pending_run] if pending_run is not None else [])]
                    await asyncio.gather(*pending, return_exceptions=True)
                    # communicate closes pipe transports even if the leader exited first.
                    await proc.communicate()
                try:
                    await asyncio.wait_for(cleanup(), timeout=CLEANUP_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    raise ChatGPTPlanError("subscription_transport", "Codex CLI process cleanup exceeded its limit; request stopped") from None

    def _exec_argv(self, catalog_path: Path, workdir: Path) -> list[str]:
        overrides = [
            'forced_login_method="chatgpt"', f'model_provider="{CLI_PROVIDER_ID}"',
            f'model_providers.{CLI_PROVIDER_ID}.name="OpenAI"',
            f'model_providers.{CLI_PROVIDER_ID}.requires_openai_auth=true',
            f'model_providers.{CLI_PROVIDER_ID}.wire_api="responses"',
            f"model_catalog_json={json.dumps(str(catalog_path))}",
            f"log_dir={json.dumps(str(workdir / 'logs'))}",
            'web_search="disabled"', "mcp_servers={}", "project_doc_max_bytes=0",
            "features.skip_host_skill_discovery=true",
            "suppress_unstable_features_warning=true",
            "agents.enabled=false", "skills.include_instructions=false", "skills.bundled.enabled=false", "cloud.skills.enabled=false",
            "tools.update_plan.enabled=false", "tools.experimental_request_user_input.enabled=false",
            f"model_providers.{CLI_PROVIDER_ID}.request_max_retries=0", f"model_providers.{CLI_PROVIDER_ID}.stream_max_retries=0",
        ]
        overrides.extend(f"features.{feature}=false" for feature in DISABLED_FEATURES)
        argv = [self.binary, "exec", "--ignore-user-config", "--strict-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "--color", "never", "--model", self.model, "--cd", str(workdir)]
        for value in overrides:
            argv.extend(["-c", value])
        argv.append("-")
        return argv

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.images:
            raise ChatGPTPlanError("subscription_input", "Codex CLI provider currently accepts text inputs only")
        environment = _child_env()
        environment["RUST_LOG"] = "off"
        with TemporaryDirectory(prefix="omnidesk-codex-cli-") as directory:
            workdir = Path(directory)
            local_argv = [self.binary, "-c", f"log_dir={json.dumps(str(workdir / 'logs'))}"]
            output, _, code = await self._run([*local_argv, "--version"], env=environment, cwd=workdir)
            version = re.search(rb"(?m)^codex(?:-cli)? ([^\s]+)\s*$", output)
            if code or version is None or version[1].decode("ascii", errors="replace") != SUPPORTED_CLI_VERSION:
                raise ChatGPTPlanError("subscription_cli_version", "Codex CLI 0.162.0-alpha.17.2 is the verified version; other versions require compatibility validation")
            output, diagnostics, code = await self._run([*local_argv, "login", "status"], env=environment, cwd=workdir)
            lines = (output + b"\n" + diagnostics).decode("utf-8", errors="replace").splitlines()
            if code or not any(line.strip().lower() == "logged in using chatgpt" for line in lines):
                raise ChatGPTPlanError("subscription_auth", "An existing official Codex CLI ChatGPT login is required")
            output, _, code = await self._run([*local_argv, "debug", "models", "--bundled"], env=environment, cwd=workdir)
            try:
                catalog = json.loads(output)
                models = catalog["models"]
                selected = [dict(model) for model in models if isinstance(model, dict) and model.get("slug") == self.model]
                if code or not isinstance(models, list) or len(selected) != 1:
                    raise ValueError
                model = selected[0]
                messages = model.get("model_messages")
                if (not isinstance(model.get("base_instructions"), str) or not isinstance(model.get("shell_type"), str)
                        or "apply_patch_tool_type" not in model
                        or (model["apply_patch_tool_type"] is not None and not isinstance(model["apply_patch_tool_type"], str))
                        or not isinstance(model.get("experimental_supported_tools"), list)
                        or not all(isinstance(tool, str) for tool in model["experimental_supported_tools"])
                        or ("tool_mode" in model and model["tool_mode"] not in ("direct", "code_mode", "code_mode_only"))
                        or model.get("multi_agent_version") not in (None, "disabled", "v1", "v2")
                        or (messages is not None and (not isinstance(messages, dict)
                            or (messages.get("instructions_template") is not None and not isinstance(messages["instructions_template"], str))))
                        or type(model.get("supports_search_tool")) is not bool):
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                raise ChatGPTPlanError("subscription_model", "The selected model or bundled metadata is incompatible; verify the model and upgrade the CLI; no replacement was attempted") from None
            model.update(shell_type="disabled", apply_patch_tool_type=None, experimental_supported_tools=[], supports_search_tool=False, tool_mode="direct", multi_agent_version="disabled",
                         include_apps_usage_instructions=False, include_plugin_usage_instructions=False, include_skills_usage_instructions=False)
            system = request.system + ("\nReturn only valid JSON, with no markdown." if request.json_mode else "")
            # The canonical template takes precedence over the legacy base field.
            # Codex maps these instructions to a developer item, separately from stdin.
            trusted_instructions = f"\n\nOmniDesk trusted system instructions:\n{system}"
            model["base_instructions"] += trusted_instructions
            if messages is not None and messages.get("instructions_template") is not None:
                messages["instructions_template"] += trusted_instructions
            catalog_path = workdir / "models.json"
            catalog_path.write_text(json.dumps({"models": [model]}, ensure_ascii=False), encoding="utf-8")
            catalog_path.chmod(0o600)
            prompt = request.user.encode("utf-8")
            output, _, code = await self._run(self._exec_argv(catalog_path, workdir), env=environment, cwd=workdir,
                                             input_data=prompt, check_events=True)
            if code:
                raise ChatGPTPlanError("subscription_response", "Codex CLI request failed; verify ChatGPT login, model access and CLI compatibility")
            text, usage, thread_id = _parse_events(output)
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=usage,
                             raw={"id": thread_id, "accepted_tool_items": 0, "transport": "codex_cli"})

    async def stream(self, request: ModelRequest):
        response = await self.complete(request)
        yield ModelDelta(sequence=1, provider=self.provider_name, model=self.model, profile=self.profile_name,
                         text=response.text, usage=response.usage, finish_reason="stop", provider_request_id=(response.raw or {}).get("id"), native=False)
