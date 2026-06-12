from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from omnidesk_agent.config import GmailConfig, SandboxConfig
from omnidesk_agent.models import schema_retry
from omnidesk_agent.models.schema_retry import StructuredOutputError, build_repair_prompt, validate_json_text
from omnidesk_agent.oauth import gmail_oauth as gmail_oauth_module
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager
from omnidesk_agent.sandbox import remote_runner, runner_server
from omnidesk_agent.sandbox.remote_runner import RemoteSandboxClient, RemoteSandboxError
from omnidesk_agent.sandbox.runner_server import RunnerConfig, SandboxRunnerHandler
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def _workspace_archive(files: dict[str, bytes] | None = None) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in (files or {"hello.py": b"print('ok')\n"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_schema_retry_error_and_fallback_paths(monkeypatch):
    with pytest.raises(StructuredOutputError, match="invalid JSON"):
        validate_json_text("{not-json")
    with pytest.raises(StructuredOutputError, match="items.0"):
        validate_json_text('{"items":[1]}', {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}})
    with pytest.raises(StructuredOutputError, match="invalid JSON schema"):
        validate_json_text("{}", {"type": 123})

    monkeypatch.setattr(schema_retry, "_load_jsonschema_validator", lambda: (None, ValueError))
    assert validate_json_text('{"kind":"ok"}', {"type": "object", "required": ["kind"]}) == {"kind": "ok"}
    with pytest.raises(StructuredOutputError, match="missing required"):
        validate_json_text("{}", {"type": "object", "required": ["kind"]})
    with pytest.raises(StructuredOutputError, match="expected array"):
        validate_json_text("{}", {"type": "array"})
    system, user = build_repair_prompt(original_text="bad", error="nope", schema={"type": "object"})
    assert "Return only valid JSON" in system
    assert "nope" in user and "bad" in user


def test_gmail_oauth_scopes_redirect_and_missing_dependency_paths(tmp_path, monkeypatch):
    cfg = GmailConfig(
        credentials_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token.json",
        allow_compose=False,
        allow_send=True,
        allow_modify=True,
        readonly=False,
        oauth_redirect_allowlist=["http://127.0.0.1/callback"],
    )
    mgr = GmailOAuthManager(cfg)
    assert mgr.scopes == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    with pytest.raises(PermissionError, match="redirect_uri"):
        mgr.build_authorization_url("http://evil/callback")
    with pytest.raises(RuntimeError, match="credentials file missing"):
        mgr.build_authorization_url("http://127.0.0.1/callback")
    with pytest.raises(RuntimeError, match="credentials file missing"):
        mgr.run_local_flow()

    cfg.credentials_file.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="google-auth-oauthlib"):
        mgr.build_authorization_url("http://127.0.0.1/callback")
    with pytest.raises(PermissionError, match="Invalid"):
        mgr.exchange_code("code", "http://127.0.0.1/callback", state="missing")
    with pytest.raises(RuntimeError, match="token is missing"):
        mgr.build_service()

    mgr.save_token_json({"token": "x"})
    monkeypatch.setattr("omnidesk_agent.oauth.gmail_oauth.EncryptionProvider.decrypt_text", lambda _self, _raw: None)
    cfg.token_file.write_text("enc:v1:gmail-token:bad", encoding="utf-8")
    assert mgr.load_token_json() == {}


def test_gmail_oauth_success_paths_with_fake_google_sdks(tmp_path, monkeypatch):
    cfg = GmailConfig(credentials_file=tmp_path / "credentials.json", token_file=tmp_path / "token.json")
    cfg.credentials_file.write_text("{}", encoding="utf-8")
    mgr = GmailOAuthManager(cfg)
    assert "https://www.googleapis.com/auth/gmail.compose" in mgr.scopes

    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "fake-token"})

    class FakeInstalledAppFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert Path(path) == cfg.credentials_file
            assert scopes == mgr.scopes
            return cls()

        def run_local_server(self, port=0):
            assert port == 9898
            return FakeCreds()

    class FakeFlow:
        credentials = FakeCreds()

        @classmethod
        def from_client_secrets_file(cls, path, scopes, redirect_uri):
            assert Path(path) == cfg.credentials_file
            assert scopes == mgr.scopes
            assert redirect_uri == "http://127.0.0.1/callback"
            return cls()

        def authorization_url(self, **kwargs):
            assert kwargs["access_type"] == "offline"
            return "http://auth", kwargs["state"]

        def fetch_token(self, code):
            assert code == "code"

    flow_mod = ModuleType("google_auth_oauthlib.flow")
    flow_mod.InstalledAppFlow = FakeInstalledAppFlow
    flow_mod.Flow = FakeFlow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", ModuleType("google_auth_oauthlib"))
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)

    assert mgr.run_local_flow(port=9898) == {"token": "fake-token"}
    auth = mgr.build_authorization_url("http://127.0.0.1/callback", state="caller-state-is-ignored")
    assert auth["authorization_url"] == "http://auth"
    assert auth["state"] != "caller-state-is-ignored"
    assert mgr.exchange_code("code", "http://127.0.0.1/callback", state=auth["state"]) == {"token": "fake-token"}

    class FakeCredentials:
        @classmethod
        def from_authorized_user_info(cls, token, scopes):
            assert token == {"token": "fake-token"}
            assert scopes == mgr.scopes
            return "creds"

    creds_mod = ModuleType("google.oauth2.credentials")
    creds_mod.Credentials = FakeCredentials
    discovery_mod = ModuleType("googleapiclient.discovery")
    discovery_mod.build = lambda service, version, credentials: {"service": service, "version": version, "credentials": credentials}
    monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", creds_mod)
    monkeypatch.setitem(sys.modules, "googleapiclient", ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery_mod)
    assert mgr.build_service() == {"service": "gmail", "version": "v1", "credentials": "creds"}

    def raise_os_error(*_args):
        raise OSError("chmod unsupported")

    monkeypatch.setattr(gmail_oauth_module.os, "chmod", raise_os_error)
    mgr.save_token_json({"token": "chmod-fallback"})
    assert mgr.load_token_json() == {"token": "chmod-fallback"}
    cfg.token_file.unlink()
    mgr._ensure_private_token_permissions()
    assert mgr.load_token_json() is None


def test_runner_server_low_level_error_paths(tmp_path, monkeypatch):
    cfg = RunnerConfig(nonce_ttl_seconds=1, allow_workspace_paths=True, allowed_workspace_root=tmp_path, default_image="python:test")
    runner_server._NONCES.clear()
    assert runner_server._constant_time_ok("Bearer token", "Bearer token")
    assert not runner_server._constant_time_ok("", "")
    assert runner_server._consume_nonce("old", 10.0, cfg)
    assert runner_server._consume_nonce("new", 12.0, cfg)
    assert "old" not in runner_server._NONCES
    runner_server._forget_nonce("new", cfg)
    assert "new" not in runner_server._NONCES

    db_cfg = RunnerConfig(nonce_db_path=tmp_path / "state" / "nonces.sqlite3", nonce_ttl_seconds=1)
    assert runner_server._consume_nonce("db-old", 10.0, db_cfg)
    assert runner_server._consume_nonce("db-new", 12.0, db_cfg)
    runner_server._forget_nonce("db-new", db_cfg)
    assert runner_server._consume_nonce("db-new", 13.0, db_cfg)

    with pytest.raises(ValueError, match="allowlist"):
        runner_server._build_docker_command({"argv": ["pytest"], "image": "evil:image"}, tmp_path, cfg)
    monkeypatch.setenv(cfg.image_allowlist_env, "python:test, other:test")
    cmd = runner_server._build_docker_command({"argv": ["pytest"], "image": "python:test", "readonly": False}, tmp_path, cfg)
    assert "rw" in cmd[cmd.index("--mount") + 1]

    with pytest.raises(ValueError, match="invalid workspace archive"):
        runner_server._safe_extract_workspace_archive(b"not-a-tar", tmp_path / "bad", cfg)
    with pytest.raises(ValueError, match="too many files"):
        runner_server._workspace_from_payload({"workspace_archive_base64": _workspace_archive({"a": b"1", "b": b"2"})}, RunnerConfig(max_archive_files=1))
    with pytest.raises(ValueError, match="exceeds maximum size"):
        runner_server._workspace_from_payload({"workspace_archive_base64": _workspace_archive({"a": b"12", "b": b"34"})}, RunnerConfig(max_archive_bytes=3, max_archive_file_bytes=10))
    workspace, tmp = runner_server._workspace_from_payload({"workspace": str(tmp_path)}, cfg)
    assert workspace == tmp_path.resolve()
    assert tmp is None


def test_runner_runtime_ready_paths(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runner_server.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_server.subprocess, "run", fake_run)
    monkeypatch.delenv("OMNIDESK_SANDBOX_READY_SMOKE", raising=False)
    ok, reason = runner_server._runtime_ready(RunnerConfig(container_runtime="docker", default_image="python:test"))
    assert ok is True and reason == "ready"

    monkeypatch.setenv("OMNIDESK_SANDBOX_READY_SMOKE", "1")
    monkeypatch.setenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", "python:test")
    ok, reason = runner_server._runtime_ready(RunnerConfig(container_runtime="docker", default_image="python:test"))
    assert ok is True and calls[-1][:5] == ["docker", "run", "--rm", "--network", "none"]

    def fail_run(argv, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(runner_server.subprocess, "run", fail_run)
    ok, reason = runner_server._runtime_ready(RunnerConfig(container_runtime="docker", default_image="python:test"))
    assert ok is False and "unavailable" in reason


def test_runner_http_handler_get_and_post_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "token")
    monkeypatch.delenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", raising=False)
    monkeypatch.setenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", "python:test")

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "docker":
            return subprocess.CompletedProcess(argv, 0, stdout="hello", stderr="err")
        return subprocess.CompletedProcess(argv, 0, stdout="docker version", stderr="")

    monkeypatch.setattr(runner_server.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_server.subprocess, "run", fake_run)
    cfg = RunnerConfig(require_hmac=False, default_image="python:test", max_output_chars=10)

    def make_handler(path: str, body: bytes = b"", headers: dict[str, str] | None = None):
        handler = object.__new__(SandboxRunnerHandler)
        handler.path = path
        handler.server = SimpleNamespace(runner_config=cfg)
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body)
        captured: list[tuple[int, dict[str, object]]] = []
        handler._json = lambda code, data: captured.append((code, data))  # type: ignore[method-assign]
        return handler, captured

    handler, captured = make_handler("/health")
    SandboxRunnerHandler.do_GET(handler)
    assert captured == [(200, {"ok": True, "service": "sandbox-runner"})]
    handler, captured = make_handler("/ready")
    SandboxRunnerHandler.do_GET(handler)
    assert captured[0][0] == 200 and captured[0][1]["reason"] == "ready"
    handler, captured = make_handler("/missing")
    SandboxRunnerHandler.do_GET(handler)
    assert captured[0][0] == 404

    body = json.dumps({"argv": ["pytest"], "workspace_archive_base64": _workspace_archive(), "request_id": "r1"}).encode()
    headers = {"authorization": "Bearer token", "content-length": str(len(body))}
    handler, captured = make_handler("/v1/run", body, headers)
    SandboxRunnerHandler.do_POST(handler)
    assert captured[0][1]["ok"] is True and captured[0][1]["request_id"] == "r1"

    bad_body = b"{}"
    handler, captured = make_handler("/v1/run", bad_body, {"authorization": "Bearer token", "content-length": str(len(bad_body))})
    SandboxRunnerHandler.do_POST(handler)
    assert captured[0][1]["exit_code"] == 126
    handler, captured = make_handler("/v1/run", b"{}", {"content-length": "2"})
    SandboxRunnerHandler.do_POST(handler)
    assert captured[0][0] == 401
    handler, captured = make_handler("/not-run", b"{}", {"authorization": "Bearer token", "content-length": "2"})
    SandboxRunnerHandler.do_POST(handler)
    assert captured[0][0] == 404

    json_handler = object.__new__(SandboxRunnerHandler)
    json_handler.wfile = io.BytesIO()
    sent: list[tuple[str, object]] = []
    json_handler.send_response = lambda code: sent.append(("status", code))  # type: ignore[method-assign]
    json_handler.send_header = lambda key, value: sent.append((key, value))  # type: ignore[method-assign]
    json_handler.end_headers = lambda: sent.append(("end", True))  # type: ignore[method-assign]
    SandboxRunnerHandler._json(json_handler, 201, {"ok": True})
    assert ("status", 201) in sent
    assert json.loads(json_handler.wfile.getvalue()) == {"ok": True}


def test_remote_sandbox_client_archive_post_and_error_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "skip.pyc").write_bytes(b"x")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("skip", encoding="utf-8")
    client = RemoteSandboxClient("http://runner")

    with pytest.raises(RemoteSandboxError, match="token"):
        client._token()
    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "token")
    assert client._token() == "token"

    archive = base64.b64decode(client._archive_workspace(workspace))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        assert tf.getnames() == ["keep.py"]
    file_archive = base64.b64decode(client._archive_workspace(workspace / "keep.py"))
    with tarfile.open(fileobj=io.BytesIO(file_archive), mode="r:gz") as tf:
        assert tf.getnames() == ["keep.py"]
    with pytest.raises(RemoteSandboxError, match="does not exist"):
        client._archive_workspace(workspace / "missing")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"ok": True, "exit_code": 0, "stdout": "ok"}).encode()

    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout):
        seen.update(dict(req.header_items()))
        return Resp()

    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", "s" * 40)
    monkeypatch.setattr(remote_runner.urllib.request, "urlopen", fake_urlopen)
    result = client._post_run({"argv": ["pytest"], "workspace_archive_base64": client._archive_workspace(workspace), "nonce": "n"}, 1)
    assert result.ok is True and result.stdout == "ok"
    assert "X-omnidesk-sandbox-signature" in seen
    assert asyncio.run(client.run_command(argv=["pytest"], workspace=workspace, timeout_seconds=1)).exit_code == 0

    def fail_http(_req, timeout):
        raise urllib.error.HTTPError("http://runner", 500, "boom", None, None)

    monkeypatch.setattr(remote_runner.urllib.request, "urlopen", fail_http)
    with pytest.raises(RemoteSandboxError, match="HTTP 500"):
        client._post_run({"argv": ["pytest"]}, 1)

    def fail_generic(_req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(remote_runner.urllib.request, "urlopen", fail_generic)
    with pytest.raises(RemoteSandboxError, match="request failed"):
        client._post_run({"argv": ["pytest"]}, 1)


def test_self_upgrade_sandbox_runner_error_and_timeout_paths(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="unsupported"):
        SandboxRunner(tmp_path, sandbox_cfg=SimpleNamespace(backend="bad", docker_image="img"))  # type: ignore[arg-type]

    async def run_cases():
        runner = SandboxRunner(tmp_path)
        empty = await runner.run([])
        assert empty.exit_code == 2

        remote_missing = SandboxRunner(tmp_path, sandbox_cfg=SandboxConfig(backend="remote_docker"))
        result = await remote_missing.run(["pytest"])
        assert result.exit_code == 2 and "runner_url" in result.output

        async def boom(self, *, argv, workspace, timeout_seconds, readonly=True):
            raise RuntimeError("runner down")

        monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "x" * 40)
        monkeypatch.setattr(remote_runner.RemoteSandboxClient, "run_command", boom)
        remote = SandboxRunner(tmp_path, sandbox_cfg=SandboxConfig(backend="remote_docker", runner_url="http://runner"))
        result = await remote.run(["pytest"])
        assert result.exit_code == 2 and "runner down" in result.output

        class SlowProc:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(10)
                return b"", None

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return None

        async def fake_exec(*_argv, **_kwargs):
            return SlowProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        timed_out = await runner.run(["pytest"], timeout=0)
        assert timed_out.exit_code == 124

    asyncio.run(run_cases())
