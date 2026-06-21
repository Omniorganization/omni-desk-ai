from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.server_routes.admin_routes import register_admin_routes
from omnidesk_agent.server_routes.agent_routes import register_agent_routes
from omnidesk_agent.server_routes.webhook_routes import register_webhook_routes


async def allow_admin(request, role="viewer"):
    request.state.seen_role = role


class Metrics:
    def render_prometheus(self):
        return "omnidesk_test_metric 1\n"


class Queue:
    def __init__(self):
        self.enqueued = []

    def stats(self):
        return {"total": len(self.enqueued), "dead_letter": 0}

    def list(self, status=None, limit=50):
        return []

    def list_dead_letters(self, limit=50):
        return [{"id": "dead"}]

    def requeue_dead_letter(self, job_id):
        if job_id == "missing":
            raise KeyError(job_id)
        if job_id == "sent":
            raise ValueError("bad state")
        return {"job_id": job_id, "status": "pending"}

    def purge_dead_letter(self, job_id):
        if job_id == "missing":
            raise KeyError(job_id)
        return {"job_id": job_id, "purged": True}

    def enqueue(self, message, source_key):
        self.enqueued.append((message, source_key))
        return {"job_id": f"job-{len(self.enqueued)}", "created": True}


class Outbound:
    def stats(self):
        return {"total": 2, "dead_letter": 0}

    def list(self, status=None, limit=50):
        return [{"id": "out", "status": status or "pending"}]

    def requeue(self, message_id):
        if message_id == "missing":
            raise KeyError(message_id)
        if message_id == "sent":
            raise ValueError("sent outbound message cannot be retried")
        return {"id": message_id, "status": "pending"}

    def cancel(self, message_id):
        if message_id == "missing":
            raise KeyError(message_id)
        if message_id == "sent":
            raise ValueError("sent outbound message cannot be cancelled")
        return {"id": message_id, "status": "cancelled"}


class Runtime:
    def __init__(self):
        self.job_queue = Queue()
        self.outbound_messages = Outbound()

    def status(self):
        return {"tools": ["shell"], "jobs": self.job_queue.stats()}


class ApprovalStore:
    def __init__(self):
        self.items = []

    def create(self, body):
        self.items.append(body)
        return "approval-1"

    def list(self, status=None):
        return self.items

    def decide(self, approval_id, decision, body):
        return {"id": approval_id, "decision": decision, "metadata": body}


class Orchestrator:
    async def handle_message(self, msg):
        return {"ok": True, "text": msg.text, "sender": msg.sender_id}

    async def resume(self, run_id, resume_token=None):
        return {"ok": True, "run_id": run_id, "resume_token": resume_token}


class Governance:
    async def evaluate_proposal(self, proposal_id, **kwargs):
        return {"proposal_id": proposal_id, "kwargs": kwargs}


class OAuth:
    def __init__(self):
        self.started = []
        self.exchanged = []

    def build_authorization_url(self, redirect_uri, state=None, *, actor=None):
        self.started.append({"redirect_uri": redirect_uri, "state": state, "actor": actor})
        return {"authorization_url": f"https://auth.example/?redirect={redirect_uri}", "state": "server-state"}

    def exchange_code(self, code, redirect_uri, state=None, *, actor=None):
        self.exchanged.append({"code": code, "redirect_uri": redirect_uri, "state": state, "actor": actor})
        if state == "bad":
            raise PermissionError("bad state")
        return {"access_token": "token", "refresh_token": "refresh"}


class GmailAdapter:
    def __init__(self):
        self.oauth = OAuth()


class AgentRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.orchestrator = Orchestrator()
        self.governance = Governance()
        self.adapters = {"gmail": GmailAdapter()}


class TelegramAdapter:
    def parse_update(self, payload):
        return ChannelMessage(channel="telegram", sender_id="u", thread_id="t", text=payload.get("text", "hi"))


class MultiAdapter:
    def __init__(self, channel):
        self.channel = channel

    def parse_webhook(self, payload):
        return [ChannelMessage(channel=self.channel, sender_id="u", text="hi")]


class WechatAdapter:
    def verify_signature(self, *args):
        return True

    def parse_xml(self, body):
        return ChannelMessage(channel="wechat", sender_id="u", text="hi")

    def passive_text_reply(self, msg, text):
        return f"<xml>{text}</xml>"


class LarkAdapter:
    def __init__(self, challenge=False):
        self.challenge = challenge

    def parse_webhook(self, payload):
        if self.challenge:
            return {"challenge": "abc"}
        return ChannelMessage(channel="lark", sender_id="u", text="hi")


class XAdapter(MultiAdapter):
    def crc_response(self, token):
        return {"response_token": token[::-1]}


class Guard:
    @staticmethod
    def json_body(body):
        return json.loads(body.decode() or "{}")

    async def guard(self, channel, adapter, request, payload=None):
        return await request.body(), object()


def test_admin_routes_cover_status_jobs_outbound_learning_and_slo(tmp_path):
    app = FastAPI()
    cfg = AppConfig()
    cfg.workspace.root = tmp_path
    rt = Runtime()
    register_admin_routes(app, cfg, rt, Metrics(), "test-version", allow_admin)
    with TestClient(app) as client:
        assert client.get("/admin/status").json()["version"] == "test-version"
        assert "omnidesk_test_metric" in client.get("/admin/metrics").text
        assert client.get("/admin/slo").json()["ok"] is True
        assert client.get("/admin/jobs").json()["ok"] is True
        assert client.get("/admin/jobs/dead-letter").json()["jobs"][0]["id"] == "dead"
        assert client.post("/admin/jobs/dead-letter/dead/requeue").json()["status"] == "pending"
        assert client.delete("/admin/jobs/dead-letter/dead").json()["purged"] is True
        assert client.get("/admin/outbound").json()["messages"][0]["id"] == "out"
        assert client.get("/admin/outbound-messages").json()["stats"]["total"] == 2
        assert client.post("/admin/outbound/out/retry").json()["status"] == "pending"
        assert client.post("/admin/outbound/out/cancel").json()["status"] == "cancelled"
        assert isinstance(client.get("/admin/learning/report").json(), dict)
        assert "Learning" in client.get("/admin/learning/dashboard").text
        assert client.post("/admin/outbound/missing/retry").status_code == 404
        assert client.post("/admin/outbound/sent/cancel").status_code == 409
        assert client.post("/admin/jobs/dead-letter/missing/requeue").status_code == 404


def test_agent_routes_cover_run_resume_oauth_approval_and_upgrade(monkeypatch):
    app = FastAPI()
    cfg = AppConfig()
    cfg.gateway.shared_secret_env = "OMNIDESK_TEST_GATEWAY_SECRET"
    monkeypatch.setenv("OMNIDESK_TEST_GATEWAY_SECRET", "shared")
    rt = AgentRuntime()
    approvals = ApprovalStore()
    register_agent_routes(app, cfg, rt, approvals, allow_admin)
    with TestClient(app) as client:
        run = client.post("/agent/run", json={"message": "hello", "secret": "shared"})
        assert run.json()["text"] == "hello"
        assert run.json()["sender"] == "operator"
        assert client.post("/agent/run", json={"message": "hello", "actor": "a", "secret": "shared"}).status_code == 422
        assert client.post("/agent/run", json={"message": "x" * 12001, "secret": "shared"}).status_code == 422
        assert client.post("/agent/run", json={"message": "hello"}).status_code == 401
        assert client.post("/agent/run", json={"message": "hello", "secret": "wrong"}).status_code == 401
        assert client.post("/agent/resume/run-1", json={"resume_token": "tok"}).json()["run_id"] == "run-1"
        assert client.post("/self-upgrade/proposals/p1/evaluate", json={"allow_canary": True}).json()["proposal_id"] == "p1"
        assert client.get("/oauth/gmail/start", params={"redirect_uri": "https://cb.example"}).json()["state"] == "server-state"
        assert client.get("/oauth/gmail/callback", params={"code": "c", "redirect_uri": "https://cb.example", "state": "ok"}).json()["token_saved"] is True
        assert client.get("/oauth/gmail/callback", params={"code": "c", "redirect_uri": "https://cb.example", "state": "bad"}).status_code == 403
        assert rt.adapters["gmail"].oauth.started == [{"redirect_uri": "https://cb.example", "state": None, "actor": "owner"}]
        assert rt.adapters["gmail"].oauth.exchanged[0]["actor"] == "owner"
        assert client.post("/approvals", json={"tool": "shell"}).json()["id"] == "approval-1"
        assert client.get("/approvals").json()["approvals"][0]["tool"] == "shell"
        assert client.post("/approvals/approval-1/approve", json={"by": "owner"}).json()["approval"]["decision"] == "approved"
        assert client.post("/approvals/approval-1/deny", json={}).json()["approval"]["decision"] == "denied"


def test_webhook_routes_cover_all_channel_enqueue_paths():
    app = FastAPI()
    cfg = AppConfig()
    rt = Runtime()
    rt.adapters = {
        "telegram": TelegramAdapter(),
        "whatsapp_cloud": MultiAdapter("whatsapp"),
        "meta_graph": MultiAdapter("meta"),
        "wechat_official": WechatAdapter(),
        "dingtalk": TelegramAdapter(),
        "lark": LarkAdapter(),
        "feishu": LarkAdapter(challenge=True),
        "line": MultiAdapter("line"),
        "x": XAdapter("x"),
    }
    rt.adapters["dingtalk"].parse_webhook = lambda payload: ChannelMessage(channel="dingtalk", sender_id="u", text="hi")
    register_webhook_routes(app, cfg, rt, Guard())
    with TestClient(app) as client:
        assert client.post("/webhooks/telegram", json={"text": "hi"}).json()["queued"] is True
        assert client.post("/webhooks/whatsapp", json={}).json()["count"] == 1
        assert client.post("/webhooks/meta", json={}).json()["count"] == 1
        assert client.post("/webhooks/wechat", content=b"<xml/>").text.startswith("<xml>")
        assert client.post("/webhooks/dingtalk", json={}).json()["queued"] is True
        assert client.post("/webhooks/lark", json={}).json()["queued"] is True
        assert client.post("/webhooks/feishu", json={}).json()["challenge"] == "abc"
        assert client.post("/webhooks/line", json={}).json()["count"] == 1
        assert client.get("/webhooks/x", params={"crc_token": "abc"}).json()["response_token"] == "cba"
        assert client.post("/webhooks/x", json={}).json()["count"] == 1
