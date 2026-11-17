"""The sandbox runner must fail closed when its bearer token is not configured.

Regression for the review finding where an unset OMNIDESK_SANDBOX_RUNNER_TOKEN
made the expected header the non-empty string "Bearer ", so a request sending
exactly "Authorization: Bearer " ran commands on a runner listening on
0.0.0.0:18890 (HMAC is optional outside production).
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnidesk_agent.sandbox import runner_server
from omnidesk_agent.sandbox.runner_server import RunnerConfig, SandboxRunnerHandler

TOKEN_ENV = "OMNIDESK_SANDBOX_RUNNER_TOKEN"


def _archive() -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("hello.py")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def runtime_calls(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="EXECUTED", stderr="")

    monkeypatch.delenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET", raising=False)
    monkeypatch.setenv("OMNIDESK_SANDBOX_IMAGE_ALLOWLIST", "python:test")
    monkeypatch.setattr(runner_server.subprocess, "run", fake_run)
    return calls


def _post(authorization: str | None):
    body = json.dumps({"argv": ["pytest"], "workspace_archive_base64": _archive()}).encode()
    handler = object.__new__(SandboxRunnerHandler)
    handler.path = "/v1/run"
    handler.server = SimpleNamespace(runner_config=RunnerConfig(require_hmac=False, default_image="python:test"))
    handler.headers = {"content-length": str(len(body))}
    if authorization is not None:
        handler.headers["authorization"] = authorization
    handler.rfile = io.BytesIO(body)
    captured: list[tuple[int, dict[str, object]]] = []
    handler._json = lambda code, data: captured.append((code, data))  # type: ignore[method-assign]
    SandboxRunnerHandler.do_POST(handler)
    return captured[0]


@pytest.mark.parametrize("configured", ["", "   ", "\t\n"])
@pytest.mark.parametrize("authorization", ["Bearer ", "Bearer", "Bearer    ", "Bearer \t\n", "", None])
def test_runner_without_token_refuses_every_request(monkeypatch, runtime_calls, configured, authorization):
    monkeypatch.setenv(TOKEN_ENV, configured)
    code, data = _post(authorization)
    assert code == 503
    assert data == {"ok": False, "error": "sandbox runner token is not configured"}
    assert runtime_calls == []  # nothing reached the container runtime


def test_runner_with_unset_token_env_refuses_bare_bearer(monkeypatch, runtime_calls):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    code, _ = _post("Bearer ")
    assert code == 503
    assert runtime_calls == []


@pytest.mark.parametrize(
    "authorization",
    ["Bearer ", "Bearer", "", "Bearer x", "Bearer s3cret-token ", "bearer s3cret-token", "Basic s3cret-token", "s3cret-token", None],
)
def test_runner_with_token_rejects_anything_but_the_exact_bearer_token(monkeypatch, runtime_calls, authorization):
    monkeypatch.setenv(TOKEN_ENV, "s3cret-token")
    code, data = _post(authorization)
    assert (code, data) == (401, {"ok": False, "error": "unauthorized"})
    assert runtime_calls == []


def test_runner_with_token_accepts_the_exact_bearer_token(monkeypatch, runtime_calls):
    monkeypatch.setenv(TOKEN_ENV, "s3cret-token")
    code, data = _post("Bearer s3cret-token")
    assert code == 200 and data["stdout"] == "EXECUTED"
    assert runtime_calls and runtime_calls[0][0] == "docker"


def test_bearer_token_check_strips_the_scheme_before_comparing():
    # The original review PoC: with no token, "Bearer " equalled the expected header.
    assert runner_server._bearer_token_ok("", "Bearer ") is False
    assert runner_server._bearer_token_ok("", "") is False
    assert runner_server._bearer_token_ok("", "Bearer x") is False
    assert runner_server._bearer_token_ok(" ", "Bearer  ") is False
    assert runner_server._bearer_token_ok("t", "Bearer ") is False
    assert runner_server._bearer_token_ok("t", "Bearer t") is True
    assert runner_server._bearer_token_ok("t", "Bearer tt") is False
    assert not hasattr(runner_server, "_constant_time_ok")


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_run_server_refuses_to_start_without_token(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(TOKEN_ENV, configured)

    def must_not_run(*args, **kwargs):
        raise AssertionError("runner must refuse before probing the runtime or binding a port")

    monkeypatch.setattr(runner_server, "require_rootless_runtime", must_not_run)
    monkeypatch.setattr(runner_server, "ThreadingHTTPServer", must_not_run)
    with pytest.raises(SystemExit, match=TOKEN_ENV):
        runner_server.run_server(host="127.0.0.1", port=0, cfg=RunnerConfig())


def test_run_server_starts_with_token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "s3cret-token")
    started: list[tuple[str, int]] = []

    class FakeServer:
        def __init__(self, address, handler):
            started.append(address)

        def serve_forever(self):
            return None

    monkeypatch.setattr(runner_server, "require_rootless_runtime", lambda cfg: None)
    monkeypatch.setattr(runner_server, "ThreadingHTTPServer", FakeServer)
    runner_server.run_server(host="127.0.0.1", port=0, cfg=RunnerConfig())
    assert started == [("127.0.0.1", 0)]


def test_every_shipped_runner_deployment_injects_a_required_token():
    # docker compose: ${VAR:?msg} aborts on unset *or empty* values.
    for path in ("deploy/sandbox-runner/docker-compose.yml", "deploy/docker/docker-compose.full.yml"):
        compose = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        runner = compose["services"]["sandbox-runner"]
        assert runner["environment"][TOKEN_ENV].startswith("${" + TOKEN_ENV + ":?"), path
    # systemd: the unit reads the env file whose template carries the token.
    unit = Path("deploy/sandbox-runner.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/omnidesk/sandbox-runner.env" in unit
    env_example = Path("deploy/sandbox-runner.env.example").read_text(encoding="utf-8")
    assert f"\n{TOKEN_ENV}=" in env_example
    # Web-first host: the runner unit shares runtime.env, generated with a random token.
    web_runner = Path("deploy/web-first/omnidesk-web-runner.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/omnidesk/web-first/runtime.env" in web_runner
    assert '"SANDBOX_RUNNER_TOKEN"' in Path("scripts/web_first_host.py").read_text(encoding="utf-8")
    # Kubernetes: the gateway (the runner's client) gets the token from the external secret.
    external = yaml.safe_load(Path("deploy/kubernetes/external-secret.yaml").read_text(encoding="utf-8"))
    assert TOKEN_ENV in {item["secretKey"] for item in external["spec"]["data"]}
