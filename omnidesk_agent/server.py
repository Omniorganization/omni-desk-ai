from __future__ import annotations
import hmac, os
from fastapi import FastAPI, Request, Response, HTTPException
from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.self_upgrade.dashboard.upgrade_dashboard import create_dashboard_router
from omnidesk_agent.security.approval_store import ApprovalStore

def create_app(cfg: AppConfig) -> FastAPI:
    rt = OmniDeskRuntime(cfg)
    approvals = ApprovalStore(cfg.workspace.root / 'approvals.sqlite3')
    app = FastAPI(title="OmniDesk Agent Gateway")

    async def _guard_webhook(channel: str, request: Request, source_key: str = "unknown", message_id: str | None = None) -> bytes:
        body = await request.body()
        if hasattr(rt, "webhook_security"):
            rt.webhook_security.guard(channel=channel, body=body, source_key=source_key, message_id=message_id)
        return body

    dashboard_router = create_dashboard_router(rt)
    if dashboard_router is not None:
        app.include_router(dashboard_router)

    @app.get("/health")
    async def health():
        return {"ok": True, **rt.status()}

    @app.post("/agent/run")
    async def run_agent(body: dict):
        secret = os.getenv(cfg.gateway.shared_secret_env, "")
        provided = body.get("secret", "")
        if secret and not hmac.compare_digest(secret, provided):
            raise HTTPException(401, "bad secret")
        msg = ChannelMessage(channel="local-api", sender_id=str(body.get("actor", "owner")), text=str(body["message"]))
        return await rt.orchestrator.handle_message(msg)


    @app.post("/agent/resume/{run_id}")
    async def resume_agent(run_id: str, body: dict | None = None):
        body = body or {}
        return await rt.orchestrator.resume(run_id, resume_token=body.get("resume_token"))

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        body = await _guard_webhook("telegram", request)
        import json as _json
        msg = rt.adapters["telegram"].parse_update(_json.loads(body.decode("utf-8") or "{}"))
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
        body = await _guard_webhook("whatsapp", request)
        import json as _json
        messages = rt.adapters["whatsapp_cloud"].parse_webhook(_json.loads(body.decode("utf-8") or "{}"))
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
        body = await _guard_webhook("meta", request)
        import json as _json
        messages = rt.adapters["meta_graph"].parse_webhook(_json.loads(body.decode("utf-8") or "{}"))
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}

    @app.get("/webhooks/wechat")
    async def wechat_verify(request: Request):
        q = request.query_params
        if rt.adapters["wechat_official"].verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
            return Response(content=q.get("echostr", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/wechat")
    async def wechat_webhook(request: Request):
        msg = rt.adapters["wechat_official"].parse_xml(await request.body())
        if not msg:
            return Response(content="success", media_type="text/plain")
        await rt.orchestrator.handle_message(msg)
        text = "已收到，并完成安全规划。需要执行发消息/点击/写文件等动作时会请求授权。"
        return Response(content=rt.adapters["wechat_official"].passive_text_reply(msg, text), media_type="application/xml")

    @app.post("/webhooks/dingtalk")
    async def dingtalk_webhook(request: Request):
        msg = rt.adapters["dingtalk"].parse_webhook(await request.json())
        return await rt.orchestrator.handle_message(msg) if msg else {"ok": True, "ignored": True}

    @app.post("/webhooks/lark")
    async def lark_webhook(request: Request):
        parsed = rt.adapters["lark"].parse_webhook(await request.json())
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        return await rt.orchestrator.handle_message(parsed) if parsed else {"ok": True, "ignored": True}

    @app.post("/webhooks/feishu")
    async def feishu_webhook(request: Request):
        parsed = rt.adapters["feishu"].parse_webhook(await request.json())
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        return await rt.orchestrator.handle_message(parsed) if parsed else {"ok": True, "ignored": True}

    @app.post("/webhooks/line")
    async def line_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("x-line-signature", "")
        if cfg.channels.line.enabled and not rt.adapters["line"].verify_signature(body, signature):
            raise HTTPException(403, "LINE signature verification failed")
        messages = rt.adapters["line"].parse_webhook(await request.json())
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}

    @app.get("/webhooks/x")
    async def x_crc(request: Request):
        return rt.adapters["x"].crc_response(request.query_params.get("crc_token", ""))

    @app.post("/webhooks/x")
    async def x_webhook(request: Request):
        messages = rt.adapters["x"].parse_webhook(await request.json())
        return {"ok": True, "count": len(messages), "results": [await rt.orchestrator.handle_message(m) for m in messages]}

    @app.get("/validate/connectors")
    async def validate_connectors_route():
        from omnidesk_agent.validation.connectors import validate_connectors
        return validate_connectors(rt)

    @app.get("/validate/extensions")
    async def validate_extensions_route():
        from omnidesk_agent.validation.extensions import validate_extensions
        return validate_extensions(rt)


    @app.get("/oauth/gmail/start")
    async def gmail_oauth_start(redirect_uri: str, state: str | None = None):
        # state is intentionally ignored by GmailOAuthManager; it always creates a one-time stored state.
        return rt.adapters["gmail"].oauth.build_authorization_url(redirect_uri=redirect_uri, state=None)

    @app.get("/oauth/gmail/callback")
    async def gmail_oauth_callback(code: str, redirect_uri: str, state: str | None = None):
        token = rt.adapters["gmail"].oauth.exchange_code(code=code, redirect_uri=redirect_uri, state=state)
        return {"ok": True, "token_saved": True, "keys": sorted(token.keys())}

    @app.post("/approvals")
    async def create_approval(body: dict):
        approval_id = approvals.create(body)
        return {"ok": True, "id": approval_id}

    @app.get("/approvals")
    async def list_approvals(status: str | None = None):
        return {"ok": True, "approvals": approvals.list(status)}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_request(approval_id: str, body: dict | None = None):
        return {"ok": True, "approval": approvals.decide(approval_id, "approved", body or {})}

    @app.post("/approvals/{approval_id}/deny")
    async def deny_request(approval_id: str, body: dict | None = None):
        return {"ok": True, "approval": approvals.decide(approval_id, "denied", body or {})}

    return app
