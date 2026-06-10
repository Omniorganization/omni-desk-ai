from __future__ import annotations
from typing import Optional

import base64
import hashlib
import hmac
import json
import os
from fastapi import FastAPI, Request, Response, HTTPException

from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.self_upgrade.dashboard.upgrade_dashboard import create_dashboard_router
from omnidesk_agent import __version__
from omnidesk_agent.observability import JsonEventLogger, MetricsRegistry, new_request_id, public_runtime_status
from omnidesk_agent.self_learning.observability.dashboard import LearningDashboard


def create_app(cfg: AppConfig) -> FastAPI:
    rt = OmniDeskRuntime(cfg)
    approvals = rt.approval_store
    app = FastAPI(title="OmniDesk Agent Gateway")
    metrics = MetricsRegistry()
    event_logger = JsonEventLogger()
    app.state.metrics = metrics

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get(cfg.observability.request_id_header) or new_request_id()
        request.state.request_id = request_id
        started = __import__("time").time()
        try:
            response = await call_next(request)
            metrics.inc("omnidesk_http_requests_total", method=request.method, path=request.url.path, status=getattr(response, "status_code", 0))
            return response
        except Exception:
            metrics.inc("omnidesk_http_errors_total", method=request.method, path=request.url.path)
            raise
        finally:
            elapsed = __import__("time").time() - started
            metrics.set("omnidesk_http_last_latency_seconds", elapsed, path=request.url.path)
            event_logger.event("http_request", request_id=request_id, method=request.method, path=request.url.path, elapsed=elapsed)


    async def _admin(request: Request) -> None:
        decision = await rt.admin_auth.verify_request(request)
        if not decision.ok:
            raise HTTPException(status_code=403, detail=decision.reason)

    def _json(body: bytes) -> dict:
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


    def _channel_cfg(channel: str):
        return {
            "telegram": cfg.channels.telegram,
            "whatsapp": cfg.channels.whatsapp_cloud,
            "meta": cfg.channels.meta_graph,
            "wechat": cfg.channels.wechat_official,
            "dingtalk": cfg.channels.dingtalk,
            "lark": cfg.channels.lark,
            "feishu": cfg.channels.feishu,
            "line": cfg.channels.line,
            "x": cfg.channels.x,
        }.get(channel)

    def _env(name: str) -> str:
        return os.getenv(name, "")

    def _require_secret(env_name: str, channel: str) -> str:
        secret = _env(env_name)
        if not secret:
            raise PermissionError(f"{channel} webhook signature secret is not configured: {env_name}")
        return secret

    def _verify_hmac_header(body: bytes, secret: str, signature: str, *, prefix: str = "sha256=") -> None:
        if not signature:
            raise PermissionError("missing webhook signature header")
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        candidate = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
        if not hmac.compare_digest(candidate, digest):
            raise PermissionError("invalid webhook signature")

    def _verify_required_webhook_signature(channel: str, adapter, request: Request, body: bytes, payload) -> None:
        channel_cfg = _channel_cfg(channel)
        if not getattr(cfg.gateway, "require_webhook_signatures", True):
            return
        if channel_cfg is None or not bool(getattr(channel_cfg, "enabled", False)):
            return

        adapter_verify = getattr(adapter, "verify_request", None)
        if callable(adapter_verify):
            adapter_verify(dict(request.headers), body, dict(request.query_params), payload)
            return

        headers = request.headers

        if channel == "telegram":
            secret = _require_secret(channel_cfg.webhook_secret_env, channel)
            provided = headers.get("x-telegram-bot-api-secret-token", "")
            if not hmac.compare_digest(provided, secret):
                raise PermissionError("invalid Telegram webhook secret token")
            return

        if channel == "whatsapp":
            secret = _require_secret(channel_cfg.app_secret_env, channel)
            _verify_hmac_header(body, secret, headers.get("x-hub-signature-256", ""), prefix="sha256=")
            return

        if channel == "meta":
            secret = _require_secret(channel_cfg.app_secret_env, channel)
            _verify_hmac_header(body, secret, headers.get("x-hub-signature-256", ""), prefix="sha256=")
            return

        if channel == "line":
            signature = headers.get("x-line-signature", "")
            if not adapter.verify_signature(body, signature):
                raise PermissionError("invalid LINE webhook signature")
            return

        if channel == "wechat":
            q = request.query_params
            if not adapter.verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
                raise PermissionError("invalid WeChat webhook signature")
            return

        if channel in {"lark", "feishu"}:
            token = getattr(adapter, "verification_token", "") or _require_secret(channel_cfg.verification_token_env, channel)
            if not isinstance(payload, dict) or payload.get("token") != token:
                raise PermissionError(f"invalid {channel} verification token")
            secret_env = getattr(channel_cfg, "webhook_secret_env", "")
            secret = _env(secret_env) if secret_env else ""
            signature = headers.get("x-omnidesk-webhook-signature-256", "")
            if secret or signature:
                _verify_hmac_header(body, secret or _require_secret(secret_env, channel), signature, prefix="sha256=")
            return

        secret_env = getattr(channel_cfg, "webhook_secret_env", "")
        secret = _require_secret(secret_env, channel)
        _verify_hmac_header(body, secret, headers.get("x-omnidesk-webhook-signature-256", ""), prefix="sha256=")


    def _envelope(adapter, payload):
        if hasattr(adapter, "extract_envelope"):
            return adapter.extract_envelope(payload)
        from omnidesk_agent.channels.base import WebhookEnvelope
        return WebhookEnvelope(raw=payload if isinstance(payload, dict) else {})

    async def _guard_webhook(channel: str, adapter, request: Request, payload=None) -> tuple[bytes, object]:
        body = await request.body()
        try:
            actual_payload = payload if payload is not None else _json(body)
            _verify_required_webhook_signature(channel, adapter, request, body, actual_payload)
            envelope = _envelope(adapter, actual_payload)
            if hasattr(rt, "webhook_security"):
                rt.webhook_security.guard(
                    channel=channel,
                    body=body,
                    source_key=getattr(envelope, "source_key", None) or "unknown",
                    message_id=getattr(envelope, "message_id", None),
                    timestamp=getattr(envelope, "timestamp", None),
                )
            return body, envelope
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    dashboard_router = create_dashboard_router(rt, admin_auth=rt.admin_auth)
    if dashboard_router is not None:
        app.include_router(dashboard_router)

    @app.get("/health")
    async def health():
        return public_runtime_status(__version__)

    @app.get("/admin/status")
    async def admin_status(request: Request):
        await _admin(request)
        return {"ok": True, "version": __version__, "runtime": rt.status()}

    @app.get("/admin/metrics")
    async def admin_metrics(request: Request):
        await _admin(request)
        return Response(content=metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

    def _learning_dashboard() -> LearningDashboard:
        path = cfg.workspace.root / "learning_audit.jsonl"
        return LearningDashboard.from_audit_path(path)

    @app.get("/admin/learning/report")
    async def admin_learning_report(request: Request, days: int = 7):
        await _admin(request)
        return _learning_dashboard().summary(days=days)

    @app.get("/admin/learning/dashboard")
    async def admin_learning_dashboard(request: Request, days: int = 7):
        await _admin(request)
        return Response(content=_learning_dashboard().render_html(days=days), media_type="text/html")

    @app.post("/agent/run")
    async def run_agent(request: Request, body: dict):
        await _admin(request)
        # Legacy body secret remains accepted for older local integrations.
        secret = os.getenv(cfg.gateway.shared_secret_env, "")
        provided = body.get("secret", "")
        if secret and provided and not hmac.compare_digest(secret, provided):
            raise HTTPException(401, "bad secret")
        msg = ChannelMessage(channel="local-api", sender_id=str(body.get("actor", "owner")), text=str(body["message"]))
        return await rt.orchestrator.handle_message(msg)

    @app.post("/agent/resume/{run_id}")
    async def resume_agent(run_id: str, request: Request, body: Optional[dict] = None):
        await _admin(request)
        body = body or {}
        return await rt.orchestrator.resume(run_id, resume_token=body.get("resume_token"))

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        adapter = rt.adapters["telegram"]
        body, _ = await _guard_webhook("telegram", adapter, request)
        msg = adapter.parse_update(_json(body))
        return await rt.orchestrator.handle_message(msg) if msg else {"ok": True, "ignored": True}

    @app.get("/webhooks/whatsapp")
    async def whatsapp_verify(request: Request):
        params = dict(request.query_params)
        verify_token = os.getenv(cfg.channels.whatsapp_cloud.verify_token_env, "")
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
            return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        adapter = rt.adapters["whatsapp_cloud"]
        body, _ = await _guard_webhook("whatsapp", adapter, request)
        messages = adapter.parse_webhook(_json(body))
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}

    @app.get("/webhooks/meta")
    async def meta_verify(request: Request):
        params = dict(request.query_params)
        verify_token = os.getenv(cfg.channels.meta_graph.verify_token_env, "")
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
            return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/meta")
    async def meta_webhook(request: Request):
        adapter = rt.adapters["meta_graph"]
        body, _ = await _guard_webhook("meta", adapter, request)
        messages = adapter.parse_webhook(_json(body))
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}

    @app.get("/webhooks/wechat")
    async def wechat_verify(request: Request):
        q = request.query_params
        if rt.adapters["wechat_official"].verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
            return Response(content=q.get("echostr", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/wechat")
    async def wechat_webhook(request: Request):
        adapter = rt.adapters["wechat_official"]
        body, _ = await _guard_webhook("wechat", adapter, request, payload=await request.body())
        msg = adapter.parse_xml(body)
        if not msg:
            return Response(content="success", media_type="text/plain")
        await rt.orchestrator.handle_message(msg)
        text = "已收到，并完成安全规划。需要执行发消息/点击/写文件等动作时会请求授权。"
        return Response(content=adapter.passive_text_reply(msg, text), media_type="application/xml")

    @app.post("/webhooks/dingtalk")
    async def dingtalk_webhook(request: Request):
        adapter = rt.adapters["dingtalk"]
        body, _ = await _guard_webhook("dingtalk", adapter, request)
        msg = adapter.parse_webhook(_json(body))
        return await rt.orchestrator.handle_message(msg) if msg else {"ok": True, "ignored": True}

    @app.post("/webhooks/lark")
    async def lark_webhook(request: Request):
        adapter = rt.adapters["lark"]
        body, _ = await _guard_webhook("lark", adapter, request)
        parsed = adapter.parse_webhook(_json(body))
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        return await rt.orchestrator.handle_message(parsed) if parsed else {"ok": True, "ignored": True}

    @app.post("/webhooks/feishu")
    async def feishu_webhook(request: Request):
        adapter = rt.adapters["feishu"]
        body, _ = await _guard_webhook("feishu", adapter, request)
        parsed = adapter.parse_webhook(_json(body))
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        return await rt.orchestrator.handle_message(parsed) if parsed else {"ok": True, "ignored": True}

    @app.post("/webhooks/line")
    async def line_webhook(request: Request):
        adapter = rt.adapters["line"]
        body, _ = await _guard_webhook("line", adapter, request)
        payload = _json(body)
        messages = adapter.parse_webhook(payload)
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}


    @app.get("/webhooks/x")
    async def x_crc(request: Request):
        return rt.adapters["x"].crc_response(request.query_params.get("crc_token", ""))

    @app.post("/webhooks/x")
    async def x_webhook(request: Request):
        adapter = rt.adapters["x"]
        body, _ = await _guard_webhook("x", adapter, request)
        messages = adapter.parse_webhook(_json(body))
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}


    @app.post("/self-upgrade/proposals/{proposal_id}/evaluate")
    async def evaluate_upgrade_proposal(proposal_id: str, request: Request, body: Optional[dict] = None):
        await _admin(request)
        body = body or {}
        return await rt.governance.evaluate_proposal(
            proposal_id,
            old_permissions=body.get("old_permissions"),
            new_permissions=body.get("new_permissions"),
            stable_plan=body.get("stable_plan"),
            shadow_plan=body.get("shadow_plan"),
        )

    @app.get("/validate/connectors")
    async def validate_connectors_route(request: Request):
        await _admin(request)
        from omnidesk_agent.validation.connectors import validate_connectors
        return validate_connectors(rt)

    @app.get("/validate/extensions")
    async def validate_extensions_route(request: Request):
        await _admin(request)
        from omnidesk_agent.validation.extensions import validate_extensions
        return validate_extensions(rt)

    @app.get("/oauth/gmail/start")
    async def gmail_oauth_start(request: Request, redirect_uri: str, state: Optional[str] = None):
        await _admin(request)
        return rt.adapters["gmail"].oauth.build_authorization_url(redirect_uri=redirect_uri, state=None)

    @app.get("/oauth/gmail/callback")
    async def gmail_oauth_callback(request: Request, code: str, redirect_uri: str, state: Optional[str] = None):
        await _admin(request)
        token = rt.adapters["gmail"].oauth.exchange_code(code=code, redirect_uri=redirect_uri, state=state)
        return {"ok": True, "token_saved": True, "keys": sorted(token.keys())}

    @app.post("/approvals")
    async def create_approval(request: Request, body: dict):
        await _admin(request)
        approval_id = approvals.create(body)
        return {"ok": True, "id": approval_id}

    @app.get("/approvals")
    async def list_approvals(request: Request, status: Optional[str] = None):
        await _admin(request)
        return {"ok": True, "approvals": approvals.list(status)}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_request(approval_id: str, request: Request, body: Optional[dict] = None):
        await _admin(request)
        return {"ok": True, "approval": approvals.decide(approval_id, "approved", body or {})}

    @app.post("/approvals/{approval_id}/deny")
    async def deny_request(approval_id: str, request: Request, body: Optional[dict] = None):
        await _admin(request)
        return {"ok": True, "approval": approvals.decide(approval_id, "denied", body or {})}

    return app
