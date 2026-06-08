from __future__ import annotations

import hmac
import os
from fastapi import FastAPI, Request, Response, HTTPException

from omnidesk_agent.channels.telegram import TelegramChannel
from omnidesk_agent.channels.whatsapp_cloud import WhatsAppCloudChannel
from omnidesk_agent.channels.wechat_official import WeChatOfficialChannel
from omnidesk_agent.channels.meta_graph import MetaGraphChannel
from omnidesk_agent.config import AppConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.daemon import OmniDeskRuntime


def create_app(cfg: AppConfig) -> FastAPI:
    rt = OmniDeskRuntime(cfg)
    app = FastAPI(title="OmniDesk Agent Gateway")

    telegram = TelegramChannel(cfg.channels.telegram)
    whatsapp = WhatsAppCloudChannel(cfg.channels.whatsapp_cloud)
    wechat = WeChatOfficialChannel(cfg.channels.wechat_official)
    meta = MetaGraphChannel(cfg.channels.meta_graph)

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

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        update = await request.json()
        msg = telegram.parse_update(update)
        if msg:
            result = await rt.orchestrator.handle_message(msg)
            # Do not auto-reply by default; sending messages is a separate high-risk action.
            return result
        return {"ok": True, "ignored": True}

    @app.get("/webhooks/whatsapp")
    async def whatsapp_verify(request: Request):
        params = dict(request.query_params)
        verify_token = os.getenv(cfg.channels.whatsapp_cloud.verify_token_env, "")
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
            return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        payload = await request.json()
        messages = whatsapp.parse_webhook(payload)
        results = [await rt.orchestrator.handle_message(m) for m in messages]
        return {"ok": True, "count": len(results), "results": results}

    @app.get("/webhooks/meta")
    async def meta_verify(request: Request):
        params = dict(request.query_params)
        verify_token = os.getenv(cfg.channels.meta_graph.verify_token_env, "")
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
            return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/meta")
    async def meta_webhook(request: Request):
        payload = await request.json()
        messages = meta.parse_webhook(payload)
        results = [await rt.orchestrator.handle_message(m) for m in messages]
        return {"ok": True, "count": len(results), "results": results}

    @app.get("/webhooks/wechat")
    async def wechat_verify(request: Request):
        q = request.query_params
        if wechat.verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
            return Response(content=q.get("echostr", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/wechat")
    async def wechat_webhook(request: Request):
        body = await request.body()
        msg = wechat.parse_xml(body)
        if not msg:
            return Response(content="success", media_type="text/plain")
        result = await rt.orchestrator.handle_message(msg)
        text = "已收到，并完成安全规划。需要执行发消息/点击/写文件等动作时会请求授权。"
        return Response(content=wechat.passive_text_reply(msg, text), media_type="application/xml")

    return app
