from __future__ import annotations

import asyncio
from omnidesk_agent.self_upgrade.sandbox_runner import SandboxRunner


def test_sandbox_runner_blocks_unlisted_command(tmp_path):
    async def run_case():
        runner = SandboxRunner(tmp_path)
        result = await runner.run("bash -c echo bad")
        assert not result.ok
        assert result.exit_code == 126

    asyncio.run(run_case())


def test_runner_server_hides_internal_errors_but_returns_validation_errors(monkeypatch):
    import io
    import json
    from types import SimpleNamespace

    from omnidesk_agent.sandbox import runner_server
    from omnidesk_agent.sandbox.runner_server import RunnerConfig, SandboxRunnerHandler

    monkeypatch.setenv("OMNIDESK_SANDBOX_RUNNER_TOKEN", "token")
    monkeypatch.setattr(runner_server, "_verify_signature", lambda headers, body, cfg: (True, "ok"))

    def post(payload):
        body = json.dumps(payload).encode()
        handler = object.__new__(SandboxRunnerHandler)
        handler.path = "/v1/run"
        handler.server = SimpleNamespace(runner_config=RunnerConfig())
        handler.headers = {"authorization": "Bearer token", "content-length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        captured = []
        handler._json = lambda code, data: captured.append((code, data))
        SandboxRunnerHandler.do_POST(handler)
        return captured[0]

    code, data = post({"argv": ["pytest"], "workspace_archive_base64": "!!not-base64!!"})
    assert code == 400

    def boom(payload, cfg):
        raise RuntimeError("/srv/secret/host/path leaked")

    monkeypatch.setattr(runner_server, "_workspace_from_payload", boom)
    code, data = post({"argv": ["pytest"]})
    assert code == 500
    assert data["stderr"] == "sandbox runner internal error"
    assert "/srv/secret" not in json.dumps(data)
