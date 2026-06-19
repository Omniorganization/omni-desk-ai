from __future__ import annotations

import base64
import hashlib
import hmac


def line_signature_valid(body: bytes, channel_secret: str, signature: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def dingtalk_signature(secret: str, timestamp_ms: str) -> str:
    msg = f"{timestamp_ms}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    arr = sorted([token, timestamp, nonce])
    # WeChat Official Account webhook verification requires SHA-1 for this
    # protocol signature. It is not used as a password hash or general MAC.
    return hashlib.sha1("".join(arr).encode("utf-8"), usedforsecurity=False).hexdigest()


def x_crc_response(crc_token: str, consumer_secret: str) -> str:
    digest = hmac.new(consumer_secret.encode("utf-8"), crc_token.encode("utf-8"), hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode("ascii")
