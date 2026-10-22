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


SUPPORTED_CLI_VERSIONS = {"0.162.0-alpha.17.2", "0.163.0-alpha.10.2"}
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


def _trusted_instruction_sources(sources: Any, environment: dict[str, str]) -> bool:
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        return False
    if not sources:
        return True
    home = environment.get("CODEX_HOME") or (str(Path(environment["HOME"]) / ".codex") if environment.get("HOME") else None)
    if not home or not Path(home).is_absolute():
        return False
    try:
        cli_home = Path(home).resolve(strict=True)
        home_mode = cli_home.stat()
        if home_mode.st_uid != os.getuid() or home_mode.st_mode & 0o022:
            return False
        for source in sources:
            path = Path(source)
            if not path.is_absolute():
                return False
            path = path.resolve(strict=True)
            mode = path.stat()
            if not path.is_file() or path.parent != cli_home or path.name not in {"AGENTS.md", "AGENTS.override.md"} or mode.st_uid != os.getuid() or mode.st_mode & 0o022:
                return False
    except OSError:
        return False
    return True


def _official_transport(config: Any) -> bool:
    if not isinstance(config, dict) or config.get("openai_base_url") is not None or config.get("chatgpt_backend_base_url") is not None:
        return False
    if config.get("chatgpt_base_url") not in ("https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/"):
        return False
    providers = config.get("model_providers")
    provider = providers.get(CLI_PROVIDER_ID) if isinstance(providers, dict) else None
    expected = {"name": "OpenAI", "wire_api": "responses", "requires_openai_auth": True,
                "request_max_retries": 0, "stream_max_retries": 0,
                "supports_websockets": False, "supports_standalone_web_search": False}
    if not isinstance(provider, dict) or any(type(provider.get(key)) is not type(value) or provider[key] != value for key, value in expected.items()):
        return False
    return all(value is None for key, value in provider.items() if key not in expected)


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

    async def _run(self, argv: list[str], *, env: dict[str, str], cwd: Path, input_data: bytes = b"",
                   request: ModelRequest | None = None, base_instructions: str | None = None):
        if os.name != "posix":
            raise ChatGPTPlanError("subscription_platform", "Codex CLI provider requires POSIX process-group cleanup; request stopped")
        proc = None
        tasks = []
        pending_run = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=MAX_OUTPUT_BYTES + 1,
            )

            async def send(message):
                proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
                await proc.stdin.drain()

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
                        raise ChatGPTPlanError("subscription_output_limit", "Codex CLI output exceeded its limit; request stopped")
                return bytes(data)

            async def session():
                await send({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "omnidesk_subscription", "version": "1"},
                    "capabilities": {"experimentalApi": False, "explicitGatewayOauth": True,
                                     "optOutNotificationMethods": ["remoteControl/status/changed", "account/updated", "account/rateLimits/updated", "skills/changed", "app/list/updated"]},
                }})
                events = []
                size = 0
                expected = 1
                thread_id = turn_id = notified_thread = None
                usage = None
                mcp_overrides = {}
                started = finished = False
                while line := await proc.stdout.readline():
                    size += len(line)
                    if size > MAX_OUTPUT_BYTES:
                        raise ChatGPTPlanError("subscription_output_limit", "Codex CLI output exceeded its limit; request stopped")
                    try:
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError
                        if "error" in message:
                            raise ChatGPTPlanError("subscription_response", "Codex App Server request failed; request stopped")
                        method = message.get("method")
                        if method is None:
                            if type(message.get("id")) is not int or message["id"] != expected:
                                raise ValueError
                            result = message["result"]
                            if not isinstance(result, dict):
                                raise ValueError
                            if expected == 1:
                                await send({"method": "initialized"})
                                await send({"id": 4, "method": "config/read", "params": {"includeLayers": False}})
                                expected = 4
                            elif expected == 4:
                                if not _official_transport(result["config"]):
                                    raise ChatGPTPlanError("subscription_config", "Codex App Server transport overrides are unsupported; request stopped")
                                servers = result["config"].get("mcp_servers", {})
                                if not isinstance(servers, dict) or any(not isinstance(name, str) or not isinstance(value, dict) for name, value in servers.items()):
                                    raise ValueError
                                mcp_overrides = {name: {"enabled": False} for name in servers}
                                await send({"id": 2, "method": "thread/start", "params": {
                                    "model": self.model, "modelProvider": CLI_PROVIDER_ID, "cwd": str(cwd),
                                    "approvalPolicy": "on-request", "sandbox": "read-only", "ephemeral": True,
                                    "baseInstructions": base_instructions, "config": {"mcp_servers": mcp_overrides},
                                    "developerInstructions": request.system + ("\nReturn only valid JSON, with no markdown." if request.json_mode else ""),
                                }})
                                expected = 2
                            elif expected == 2:
                                thread_id = result["thread"]["id"]
                                if (not isinstance(thread_id, str) or not thread_id.strip()
                                        or result["thread"].get("ephemeral") is not True
                                        or result.get("model") != self.model or result.get("modelProvider") != CLI_PROVIDER_ID
                                        or result.get("cwd") != str(cwd) or result.get("sandbox", {}).get("type") != "readOnly"
                                        or result.get("approvalPolicy") != "on-request" or not _trusted_instruction_sources(result.get("instructionSources"), env)
                                        or (notified_thread is not None and notified_thread != thread_id)):
                                    raise ValueError
                                events.append({"type": "thread.started", "thread_id": thread_id})
                                await send({"id": 3, "method": "turn/start", "params": {
                                    "threadId": thread_id, "model": self.model,
                                    "input": [{"type": "text", "text": request.user}],
                                }})
                                expected = 3
                            elif expected == 3:
                                if result["turn"].get("status") != "inProgress":
                                    raise ValueError
                                value = result["turn"]["id"]
                                if not isinstance(value, str) or not value.strip() or (turn_id is not None and value != turn_id):
                                    raise ValueError
                                turn_id = value
                                expected = 0
                            else:
                                raise ValueError
                            continue
                        if not isinstance(method, str) or "id" in message:
                            raise ChatGPTPlanError("subscription_tools", "Codex App Server requested an unsupported action; request stopped")
                        params = message.get("params")
                        if not isinstance(params, dict):
                            raise ValueError
                        if method == "error":
                            raise ChatGPTPlanError("subscription_response", "Codex App Server request failed; request stopped")
                        if method == "thread/started":
                            value = params["thread"]["id"]
                            if notified_thread is not None or not isinstance(value, str) or not value.strip() or (thread_id is not None and value != thread_id):
                                raise ValueError
                            notified_thread = value
                            continue
                        if params.get("threadId") != thread_id or thread_id is None or finished:
                            raise ValueError
                        if method == "thread/status/changed":
                            continue
                        value = params.get("turnId", params.get("turn", {}).get("id"))
                        if not isinstance(value, str) or not value.strip() or (turn_id is not None and value != turn_id):
                            raise ValueError
                        turn_id = value
                        if method == "turn/started":
                            if started or params["turn"].get("status") != "inProgress":
                                raise ValueError
                            started = True
                            events.append({"type": "turn.started"})
                        elif not started:
                            raise ValueError
                        elif method == "thread/tokenUsage/updated":
                            last = params["tokenUsage"]["last"]
                            fields = {"input_tokens": "inputTokens", "output_tokens": "outputTokens", "cached_input_tokens": "cachedInputTokens"}
                            if any(type(last.get(field)) is not int or last[field] < 0 for field in fields.values()):
                                raise ValueError
                            usage = {name: last[field] for name, field in fields.items()}
                        elif method in {"item/started", "item/completed"}:
                            item = params["item"]
                            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                                raise ValueError
                            kind = item.get("type")
                            if kind not in {"agentMessage", "reasoning", "userMessage"}:
                                raise ChatGPTPlanError("subscription_tools", "Codex App Server attempted a tool or unsupported item; request stopped")
                            if kind == "agentMessage" and method == "item/completed":
                                if item.get("phase") not in (None, "final_answer"):
                                    raise ValueError
                                events.append({"type": "item.completed", "item": {"type": "agent_message", "text": item.get("text")}})
                        elif method in {"item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded", "item/reasoning/textDelta"}:
                            continue
                        elif method == "turn/completed":
                            turn = params["turn"]
                            if expected != 0 or turn.get("status") != "completed" or turn.get("error") is not None or usage is None:
                                raise ValueError
                            items = turn.get("items")
                            if not isinstance(items, list) or any(not isinstance(item, dict) or item.get("type") not in {"agentMessage", "reasoning", "userMessage"} for item in items):
                                raise ChatGPTPlanError("subscription_tools", "Codex App Server returned unsupported final items; request stopped")
                            events.append({"type": "turn.completed", "usage": usage})
                            finished = True
                            proc.stdin.close()
                        else:
                            raise ChatGPTPlanError("subscription_protocol", "Codex App Server returned an unsupported notification; request stopped")
                    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
                        raise ChatGPTPlanError("subscription_protocol", "Codex App Server returned invalid protocol messages; request stopped") from None
                if not finished:
                    raise ChatGPTPlanError("subscription_incomplete", "Codex App Server ended without a completed turn; request stopped")
                return b"\n".join(json.dumps(event).encode("utf-8") for event in events)

            coroutines = (write_input(), read_output(proc.stdout), read_output(proc.stderr), proc.wait()) if request is None else (session(), read_output(proc.stderr), proc.wait())
            tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
            pending_run = asyncio.gather(*tasks)
            result = await asyncio.wait_for(pending_run, timeout=TIMEOUT_SECONDS)
            return (result[1], result[2], result[3]) if request is None else (result[0], result[1], result[2])
        except asyncio.TimeoutError:
            raise ChatGPTPlanError("subscription_transport", "Codex CLI timed out; request stopped") from None
        except ValueError:
            raise ChatGPTPlanError("subscription_output_limit", "Codex CLI output exceeded its line limit; request stopped") from None
        except (OSError, BrokenPipeError, ConnectionResetError):
            raise ChatGPTPlanError("subscription_unavailable", "Codex CLI could not start or disconnected; verify the installed executable") from None
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
                    await proc.communicate()
                try:
                    await asyncio.wait_for(cleanup(), timeout=CLEANUP_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    raise ChatGPTPlanError("subscription_transport", "Codex CLI process cleanup exceeded its limit; request stopped") from None

    def _exec_argv(self, workdir: Path, catalog_path: Path) -> list[str]:
        overrides = [
            'forced_login_method="chatgpt"', f'model_provider="{CLI_PROVIDER_ID}"',
            f'model_providers.{CLI_PROVIDER_ID}.name="OpenAI"',
            f'model_providers.{CLI_PROVIDER_ID}.requires_openai_auth=true',
            f'model_providers.{CLI_PROVIDER_ID}.wire_api="responses"',
            f'model_providers.{CLI_PROVIDER_ID}.supports_websockets=false',
            f'model_providers.{CLI_PROVIDER_ID}.supports_standalone_web_search=false',
            f"log_dir={json.dumps(str(workdir / 'logs'))}",
            f'model_catalog_json={json.dumps(str(catalog_path))}', 'web_search="disabled"', "mcp_servers={}", "project_doc_max_bytes=0",
            'history.persistence="none"', 'analytics.enabled=false', 'developer_instructions=""',
            'otel.exporter="none"', 'otel.trace_exporter="none"', 'otel.log_user_prompt=false',
            "features.skip_host_skill_discovery=true",
            "suppress_unstable_features_warning=true",
            "agents.enabled=false", "skills.include_instructions=false", "skills.bundled.enabled=false", "cloud.skills.enabled=false",
            "tools.update_plan.enabled=false", "tools.experimental_request_user_input.enabled=false",
            f"model_providers.{CLI_PROVIDER_ID}.request_max_retries=0", f"model_providers.{CLI_PROVIDER_ID}.stream_max_retries=0",
        ]
        overrides.extend(f"features.{feature}=false" for feature in DISABLED_FEATURES)
        argv = [self.binary, "app-server", "--strict-config", "--listen", "stdio://"]
        for value in overrides:
            argv.extend(["-c", value])
        return argv

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.images:
            raise ChatGPTPlanError("subscription_input", "Codex CLI provider currently accepts text inputs only")
        environment = _child_env()
        environment["RUST_LOG"] = "off"
        with TemporaryDirectory(prefix="omnidesk-codex-cli-") as directory:
            workdir = Path(directory).resolve()
            local_argv = [self.binary, "-c", f"log_dir={json.dumps(str(workdir / 'logs'))}"]
            output, _, code = await self._run([*local_argv, "--version"], env=environment, cwd=workdir)
            version = re.search(rb"(?m)^codex(?:-cli)? ([^\s]+)\s*$", output)
            if code or version is None or version[1].decode("ascii", errors="replace") not in SUPPORTED_CLI_VERSIONS:
                raise ChatGPTPlanError("subscription_cli_version", "Codex CLI version is unverified; compatibility validation is required")
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
            # Only public bundled metadata reaches disk; private instructions use JSON-RPC stdin.
            catalog_path = workdir / "models.json"
            catalog_path.write_text(json.dumps({"models": [model]}, ensure_ascii=False), encoding="utf-8")
            catalog_path.chmod(0o600)
            canonical = messages.get("instructions_template") if messages is not None else None
            output, _, code = await self._run(self._exec_argv(workdir, catalog_path), env=environment, cwd=workdir,
                                             request=request, base_instructions=canonical or model["base_instructions"])
            if code:
                raise ChatGPTPlanError("subscription_response", "Codex CLI request failed; verify ChatGPT login, model access and CLI compatibility")
            text, usage, thread_id = _parse_events(output)
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=usage,
                             raw={"id": thread_id, "accepted_tool_items": 0, "transport": "codex_cli"})

    async def stream(self, request: ModelRequest):
        response = await self.complete(request)
        yield ModelDelta(sequence=1, provider=self.provider_name, model=self.model, profile=self.profile_name,
                         text=response.text, usage=response.usage, finish_reason="stop", provider_request_id=(response.raw or {}).get("id"), native=False)
