#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Optional


def request_json(url: str, token: Optional[str] = None) -> dict:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310 - operator-provided smoke URL
        body = response.read().decode("utf-8")
    return json.loads(body)


def main() -> int:
    base_url = os.getenv("OMNIDESK_SMOKE_BASE_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("OMNIDESK_SMOKE_ADMIN_TOKEN")
    health = request_json(f"{base_url}/health")
    if not health.get("ok"):
        print(f"health check failed: {health}", file=sys.stderr)
        return 1
    if token:
        status = request_json(f"{base_url}/admin/status", token)
        if not status.get("ok"):
            print(f"admin status failed: {status}", file=sys.stderr)
            return 1
    print(json.dumps({"ok": True, "health": health}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
