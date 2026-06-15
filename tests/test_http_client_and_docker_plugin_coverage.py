from __future__ import annotations

import asyncio
import pytest

from omnidesk_agent.channels import http_client
from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError, _require_httpx
from omnidesk_agent.plugins.docker_runner import DockerPluginTool
from omnidesk_agent.security.permissions import PermissionDecision
from omnidesk_agent.tools.base import ToolContext


class FakeResponse:
    def __init__(self, status_code=200, text='{"ok": true}', headers=None, json_error=False):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return {"ok": True, "status": self.status_code}


class FakeTimeout(Exception):
    pass


class FakeNetwork(Exception):
    pass


class FakeTransport(Exception):
    pass


class FakeHttpxModule:
    TimeoutException = FakeTimeout
    NetworkError = FakeNetwork
    TransportError = FakeTransport

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def AsyncClient(self, timeout):
        module = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, params=None, json=None):
                module.calls.append((method, url, headers, params, json))
                item = module.responses.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        return Client()


async def no_sleep(delay):
    return None


def test_channel_http_client_success_retry_error_and_no_httpx(monkeypatch):
    async def run_case():
        monkeypatch.setattr(http_client.asyncio, "sleep", no_sleep)
        module = FakeHttpxModule([
            FakeResponse(429, "retry", {"retry-after": "0"}),
            FakeResponse(200, "not-json", {"x-line-request-id": "rid"}, json_error=True),
        ])
        monkeypatch.setattr(http_client, "httpx", module)
        result = await ChannelHttpClient(max_retries=1, base_backoff=0).post("https://provider.test", json={"a": 1})
        assert result.status_code == 200
        assert result.data is None
        assert result.request_id == "rid"
        assert len(module.calls) == 2

        module = FakeHttpxModule([FakeResponse(400, "bad", {"x-request-id": "req"})])
        monkeypatch.setattr(http_client, "httpx", module)
        with pytest.raises(ChannelHttpError) as exc:
            await ChannelHttpClient(max_retries=0).get("https://provider.test")
        assert exc.value.status_code == 400
        assert exc.value.request_id == "req"
        assert exc.value.response_text == "bad"

        module = FakeHttpxModule([FakeNetwork("down")])
        monkeypatch.setattr(http_client, "httpx", module)
        with pytest.raises(ChannelHttpError, match="network error"):
            await ChannelHttpClient(max_retries=0).get("https://provider.test")

        monkeypatch.setattr(http_client, "httpx", None)
        with pytest.raises(RuntimeError):
            _require_httpx()

    asyncio.run(run_case())


class Metrics:
    def __init__(self):
        self.events = []

    def inc(self, name, **labels):
        self.events.append((name, labels))


class Perms:
    def __init__(self):
        self.metrics = Metrics()

    def verify(self, proposal):
        return PermissionDecision(True, "allow", "ok")





class Proc:
    def __init__(self, returncode=0, stdout=b'{"ok": true}', stderr=b''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    async def communicate(self, payload):
        self.payload = payload
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def test_docker_plugin_tool_result_branches(monkeypatch, tmp_path):
    async def run_case():
        entry = tmp_path / "plugin.py"
        entry.write_text("print('x')", encoding="utf-8")
        ctx = ToolContext(permissions=Perms(), source="test", actor="owner")
        tool = DockerPluginTool("p", entry, ["plugin.call"], max_output_bytes=200)
        with pytest.raises(ValueError):
            await tool.call("bad", {}, ctx)

        proc = Proc(stdout=b'{"answer": 1}')

        async def fake_exec_ok(*a, **k):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_ok)
        ok = await tool.call("call", {"plugin_action": "echo", "plugin_args": {"x": 1}}, ctx)
        assert ok.ok and ok.data["answer"] == 1
        assert b'"plugin_action"' not in proc.payload  # payload uses action/plugin_args contract

        async def fake_exec_plain(*a, **k):
            return Proc(stdout=b"plain")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_plain)
        plain = await tool.call("call", {"plugin_action": "echo"}, ctx)
        assert plain.ok and plain.data["stdout"] == "plain"

        async def fake_exec_large(*a, **k):
            return Proc(stdout=b"x" * 20)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_large)
        tiny_tool = DockerPluginTool("p", entry, ["plugin.call"], max_output_bytes=10)
        too_big = await tiny_tool.call("call", {"plugin_action": "echo"}, ctx)
        assert not too_big.ok and "output" in too_big.error

        async def fake_exec_fail(*a, **k):
            return Proc(returncode=2, stderr=b"boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_fail)
        failed = await tool.call("call", {"plugin_action": "echo"}, ctx)
        assert not failed.ok and failed.error == "boom"
        assert any(labels.get("status") == "error" for _, labels in ctx.permissions.metrics.events)

    asyncio.run(run_case())
