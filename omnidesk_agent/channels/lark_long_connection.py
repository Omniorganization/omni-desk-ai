"""Feishu / Lark long-connection event subscription (WebSocket mode).

The app's own credentials (``POST /callback/ws/endpoint`` with AppID and
AppSecret) return a one-time ``wss://`` URL, so no public URL is needed. The
socket carries protobuf ``pbbp2.Frame`` messages: control frames (``ping``
from the client, ``pong`` back, optionally with new client settings) and data
frames whose payload is the same schema 2.0 event JSON an HTTP push would
carry (plaintext; the connection itself is authenticated). A large event can
arrive in ``sum`` fragments under one ``message_id``. Each event must be
acknowledged within three seconds by echoing its frame with a ``biz_rt``
header and payload ``{"code": 200}``, otherwise the platform redelivers it.
Events go through ``parse_webhook``, so the open_id allowlist applies.

Protocol per the official SDKs (larksuite/oapi-sdk-python ``lark_oapi/ws``,
oapi-sdk-go ``ws``); the developer console only saves "receive events through
a long connection" while a connection is online.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from omnidesk_agent.channels.http_client import ChannelHttpClient, ChannelHttpError

FEISHU_WS_ENDPOINT = "https://open.feishu.cn/callback/ws/endpoint"
LARK_WS_ENDPOINT = "https://open.larksuite.com/callback/ws/endpoint"
METHOD_CONTROL = 0
METHOD_DATA = 1
MAX_PENDING_FRAGMENTS = 64

# --------------------------------------------------------------------------- pbbp2.Frame codec
# message Header { required string key = 1; required string value = 2; }
# message Frame { required uint64 SeqID = 1; required uint64 LogID = 2;
#   required int32 service = 3; required int32 method = 4; repeated Header headers = 5;
#   optional string payload_encoding = 6; optional string payload_type = 7;
#   optional bytes payload = 8; optional string LogIDNew = 9; }


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1  # int32/int64 negatives use ten-byte two's complement
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _field(number: int, wire: int) -> bytes:
    return _varint((number << 3) | wire)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _field(number, 2) + _varint(len(value)) + value


def encode_frame(frame: dict[str, Any]) -> bytes:
    out = bytearray()
    # Required fields are always written, even when zero.
    for number, key in ((1, "SeqID"), (2, "LogID"), (3, "service"), (4, "method")):
        out += _field(number, 0) + _varint(int(frame.get(key) or 0))
    for name, value in frame.get("headers") or []:
        out += _bytes_field(5, _bytes_field(1, str(name).encode("utf-8")) + _bytes_field(2, str(value).encode("utf-8")))
    for number, key in ((6, "payload_encoding"), (7, "payload_type")):
        if frame.get(key) is not None:
            out += _bytes_field(number, str(frame[key]).encode("utf-8"))
    if frame.get("payload") is not None:
        out += _bytes_field(8, bytes(frame["payload"]))
    if frame.get("LogIDNew") is not None:
        out += _bytes_field(9, str(frame["LogIDNew"]).encode("utf-8"))
    return bytes(out)


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value, pos = data[pos:pos + length], pos + length
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, value


def decode_frame(data: bytes) -> dict[str, Any]:
    frame: dict[str, Any] = {"SeqID": 0, "LogID": 0, "service": 0, "method": 0, "headers": []}
    names = {1: "SeqID", 2: "LogID", 3: "service", 4: "method"}
    for number, value in _fields(data):
        if number in names:
            frame[names[number]] = int(value)
        elif number == 5:
            header = dict(_fields(value))
            frame["headers"].append((bytes(header.get(1, b"")).decode("utf-8"), bytes(header.get(2, b"")).decode("utf-8")))
        elif number == 6:
            frame["payload_encoding"] = bytes(value).decode("utf-8")
        elif number == 7:
            frame["payload_type"] = bytes(value).decode("utf-8")
        elif number == 8:
            frame["payload"] = bytes(value)
        elif number == 9:
            frame["LogIDNew"] = bytes(value).decode("utf-8")
    return frame


def _header(frame: dict[str, Any], name: str) -> str:
    for key, value in frame.get("headers") or []:
        if key == name:
            return value
    return ""


def _default_connect(url: str):
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=15, close_timeout=5, max_size=2**22)


class LarkLongConnection:
    def __init__(
        self,
        channel: Any,
        deliver: Callable[[Any], Any],
        *,
        http: Optional[ChannelHttpClient] = None,
        connect: Callable[[str], Any] = _default_connect,
    ):
        self.channel = channel
        self.deliver = deliver
        self.http = http or ChannelHttpClient()
        self.connect = connect
        self.endpoint = FEISHU_WS_ENDPOINT if getattr(channel, "name", "") == "feishu" else LARK_WS_ENDPOINT
        self.ping_interval = 120.0
        self.acknowledged = 0
        self._fragments: dict[str, list[Optional[bytes]]] = {}

    async def open(self) -> tuple[str, int]:
        if not self.channel.app_id or not self.channel.app_secret:
            raise RuntimeError(f"{self.channel.name} app id/secret missing")
        result = await self.http.post(
            self.endpoint,
            headers={"locale": "zh"},
            json={"AppID": self.channel.app_id, "AppSecret": self.channel.app_secret},
            channel=self.channel.name,
            idempotent=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("code") != 0:
            raise ChannelHttpError(
                f"{self.channel.name} long-connection endpoint refused: code={data.get('code')} {str(data.get('msg') or '')[:200]}",
                channel=self.channel.name, request_sent=True, category="config_error",
            )
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        url = body.get("URL")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise ChannelHttpError(f"{self.channel.name} long-connection endpoint returned no wss:// URL", channel=self.channel.name, request_sent=True, category="config_error")
        self._apply_client_config(body.get("ClientConfig"))
        service_id = parse_qs(urlsplit(url).query).get("service_id", ["0"])[0]
        return url, int(service_id) if service_id.isdigit() else 0

    def _apply_client_config(self, config: Any) -> None:
        if isinstance(config, dict):
            interval = config.get("PingInterval")
            if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
                self.ping_interval = float(interval)

    @staticmethod
    def ping_frame(service_id: int) -> bytes:
        return encode_frame({"SeqID": 0, "LogID": 0, "service": service_id, "method": METHOD_CONTROL, "headers": [("type", "ping")]})

    async def handle_frame(self, websocket: Any, raw: bytes) -> str:
        """Process one frame: apply a pong's settings, or acknowledge and deliver an event."""
        frame = decode_frame(raw)
        frame_type = _header(frame, "type")
        if frame["method"] == METHOD_CONTROL:
            if frame_type == "pong" and frame.get("payload"):
                try:
                    self._apply_client_config(json.loads(frame["payload"]))
                except ValueError:
                    pass
            return frame_type
        if frame_type != "event":
            return frame_type  # card callbacks are not acknowledged
        payload = self._reassemble(frame)
        if payload is None:
            return "fragment"
        started = time.monotonic()
        code = 200
        try:
            parsed = self.channel.parse_webhook(json.loads(payload))
            if parsed is not None and not isinstance(parsed, dict):
                outcome = self.deliver(parsed)
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception:  # a broken event must not stall the socket; the platform may redeliver it
            code = 500
        ack = dict(frame)
        ack["headers"] = list(frame["headers"]) + [("biz_rt", str(int((time.monotonic() - started) * 1000)))]
        ack["payload"] = json.dumps({"code": code}).encode("utf-8")
        await websocket.send(encode_frame(ack))
        self.acknowledged += 1
        return frame_type

    def _reassemble(self, frame: dict[str, Any]) -> Optional[bytes]:
        total = int(_header(frame, "sum") or 1)
        if total <= 1:
            return frame.get("payload") or b""
        message_id = _header(frame, "message_id")
        index = int(_header(frame, "seq") or 0)
        if len(self._fragments) >= MAX_PENDING_FRAGMENTS and message_id not in self._fragments:
            self._fragments.pop(next(iter(self._fragments)))
        slots = self._fragments.setdefault(message_id, [None] * total)
        if 0 <= index < len(slots):
            slots[index] = frame.get("payload") or b""
        if any(part is None for part in slots):
            return None
        del self._fragments[message_id]
        return b"".join(part or b"" for part in slots)

    async def run_until(self, done: Callable[[], bool], *, timeout_seconds: float, on_connected: Optional[Callable[[], Any]] = None) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        url, service_id = await self.open()
        async with self.connect(url) as websocket:
            await websocket.send(self.ping_frame(service_id))
            if on_connected is not None:
                outcome = on_connected()
                if inspect.isawaitable(outcome):
                    await outcome
            next_ping = loop.time() + self.ping_interval
            while not done():
                now = loop.time()
                if now >= deadline:
                    return False
                if now >= next_ping:
                    await websocket.send(self.ping_frame(service_id))
                    next_ping = now + self.ping_interval
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.05, min(deadline, next_ping) - now))
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    await self.handle_frame(websocket, bytes(raw))
        return done()
