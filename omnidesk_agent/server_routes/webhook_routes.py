from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, HTTPException, Request, Response

from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.server_routes.webhook_guard import WebhookGuard, enqueue_webhook_message, enqueue_webhook_messages


def register_webhook_routes(app: FastAPI, cfg, rt, guard: WebhookGuard) -> None:
    async def _guard_webhook(channel: str, adapter, request: Request, payload=None):
        return await guard.guard(channel, adapter, request, payload=payload)

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        adapter = rt.adapters["telegram"]
        body, _ = await _guard_webhook("telegram", adapter, request)
        msg = adapter.parse_update(guard.json_body(body))
        return enqueue_webhook_message(rt, msg)

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
        messages = adapter.parse_webhook(guard.json_body(body))
        return enqueue_webhook_messages(rt, messages)

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
        messages = adapter.parse_webhook(guard.json_body(body))
        return enqueue_webhook_messages(rt, messages)

    @app.get("/webhooks/wechat")
    async def wechat_verify(request: Request):
        q = request.query_params
        if rt.adapters["wechat_official"].verify_signature(q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", "")):
            return Response(content=q.get("echostr", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/wechat")
    async def wechat_webhook(request: Request):
        adapter = rt.adapters["wechat_official"]
        raw_body = await request.body()
        body, _ = await _guard_webhook("wechat", adapter, request, payload=raw_body)
        msg = adapter.parse_xml(body)
        if not msg:
            return Response(content="success", media_type="text/plain")
        enqueue_webhook_message(rt, msg)
        text = "已收到，消息已进入异步处理队列。需要执行发消息/点击/写文件等动作时会请求授权。"
        return Response(content=adapter.passive_text_reply(msg, text), media_type="application/xml")

    @app.post("/webhooks/dingtalk")
    async def dingtalk_webhook(request: Request):
        adapter = rt.adapters["dingtalk"]
        body, _ = await _guard_webhook("dingtalk", adapter, request)
        msg = adapter.parse_webhook(guard.json_body(body))
        return enqueue_webhook_message(rt, msg)

    @app.post("/webhooks/lark")
    async def lark_webhook(request: Request):
        adapter = rt.adapters["lark"]
        body, _ = await _guard_webhook("lark", adapter, request)
        parsed = adapter.parse_webhook(guard.json_body(body))
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        if isinstance(parsed, ChannelMessage):
            return enqueue_webhook_message(rt, parsed)
        return {"ok": True, "ignored": True}

    @app.post("/webhooks/feishu")
    async def feishu_webhook(request: Request):
        adapter = rt.adapters["feishu"]
        body, _ = await _guard_webhook("feishu", adapter, request)
        parsed = adapter.parse_webhook(guard.json_body(body))
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        if isinstance(parsed, ChannelMessage):
            return enqueue_webhook_message(rt, parsed)
        return {"ok": True, "ignored": True}

    @app.post("/webhooks/line")
    async def line_webhook(request: Request):
        adapter = rt.adapters["line"]
        body, _ = await _guard_webhook("line", adapter, request)
        messages = adapter.parse_webhook(guard.json_body(body))
        return enqueue_webhook_messages(rt, messages)

    @app.get("/webhooks/x")
    async def x_crc(request: Request):
        return rt.adapters["x"].crc_response(request.query_params.get("crc_token", ""))

    @app.post("/webhooks/x")
    async def x_webhook(request: Request):
        adapter = rt.adapters["x"]
        body, _ = await _guard_webhook("x", adapter, request)
        messages = adapter.parse_webhook(guard.json_body(body))
        return enqueue_webhook_messages(rt, messages)
