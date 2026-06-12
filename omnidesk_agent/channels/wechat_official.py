from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

from omnidesk_agent.config import WeChatOfficialConfig
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.channels.http_client import ChannelHttpClient


class WeChatOfficialChannel:
    """WeChat Official Account adapter.

    Supports URL verification, XML message parsing, passive text reply, and customer-service
    message sending. Encrypted-message mode should be added before production if enabled.
    """

    name = "wechat_official"
    def extract_envelope(self, payload: bytes):
        from omnidesk_agent.channels.base import WebhookEnvelope
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(payload)
            sender = str(root.findtext("FromUserName") or "unknown")
            mid = str(root.findtext("MsgId") or "")
            ts = float(root.findtext("CreateTime") or 0) or None
            return WebhookEnvelope(source_key=sender, sender_id=sender, message_id=mid or None, timestamp=ts, raw={})
        except Exception:
            return WebhookEnvelope()

    def __init__(self, cfg: WeChatOfficialConfig):
        self.cfg = cfg
        self.token = os.getenv(cfg.token_env, "")
        self.app_id = os.getenv(cfg.app_id_env, "")
        self.app_secret = os.getenv(cfg.app_secret_env, "")
        self._access_token: Optional[tuple[str, float]] = None
        self.http = ChannelHttpClient()

    def verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        raw = "".join(sorted([self.token, timestamp, nonce]))
        digest = hashlib.sha1(raw.encode()).hexdigest()
        return digest == signature


    def verify_request(self, headers: dict[str, str], body: bytes, query_params: dict[str, str], payload) -> None:
        if not self.verify_signature(query_params.get("signature", ""), query_params.get("timestamp", ""), query_params.get("nonce", "")):
            raise PermissionError("invalid WeChat webhook signature")

    def parse_xml(self, xml_body: bytes) -> Optional[ChannelMessage]:
        root = ET.fromstring(xml_body)
        msg_type = root.findtext("MsgType")
        if msg_type != "text":
            return None
        from_user = root.findtext("FromUserName") or ""
        if self.cfg.allowed_open_ids and from_user not in self.cfg.allowed_open_ids:
            return None
        return ChannelMessage(
            channel=self.name,
            sender_id=from_user,
            thread_id=from_user,
            message_id=root.findtext("MsgId"),
            text=root.findtext("Content") or "",
            raw={child.tag: child.text for child in root},
        )

    def passive_text_reply(self, incoming: ChannelMessage, text: str) -> str:
        to_user = incoming.sender_id
        from_user = incoming.raw.get("ToUserName", "")
        now = int(time.time())
        return f"""<xml><ToUserName><![CDATA[{to_user}]]></ToUserName><FromUserName><![CDATA[{from_user}]]></FromUserName><CreateTime>{now}</CreateTime><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[{text}]]></Content></xml>"""

    async def _get_access_token(self) -> str:
        if self._access_token and self._access_token[1] > time.time() + 60:
            return self._access_token[0]
        result = await self.http.get("https://api.weixin.qq.com/cgi-bin/token", params={
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
        })
        data = result.data or {}
        token = data["access_token"]
        self._access_token = (token, time.time() + int(data.get("expires_in", 7200)))
        return token

    async def send_text(self, recipient_openid: str, text: str, **kwargs) -> None:
        token = await self._get_access_token()
        body = {"touser": recipient_openid, "msgtype": "text", "text": {"content": text}}
        await self.http.post("https://api.weixin.qq.com/cgi-bin/message/custom/send", params={"access_token": token}, json=body)
