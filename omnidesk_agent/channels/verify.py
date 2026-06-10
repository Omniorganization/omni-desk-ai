from __future__ import annotations

import hashlib
import hmac
import os
from typing import Mapping, Optional


def env_secret(env_name: str, *, channel: str) -> str:
    value = os.getenv(env_name, "")
    if not value:
        raise PermissionError(f"{channel} webhook signature secret is not configured: {env_name}")
    return value


def verify_hmac_sha256(body: bytes, secret: str, signature: str, *, prefix: str = "sha256=") -> None:
    if not signature:
        raise PermissionError("missing webhook signature header")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    candidate = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
    if not hmac.compare_digest(candidate, digest):
        raise PermissionError("invalid webhook signature")


def header(headers: Mapping[str, str], name: str) -> str:
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return ""
