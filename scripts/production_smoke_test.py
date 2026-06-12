#!/usr/bin/env python3
from __future__ import annotations

import base64
import io
import tarfile
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
import urllib.request
from typing import Optional



def build_smoke_workspace_archive() -> str:
    buf = io.BytesIO()
    content = b"print('omnidesk sandbox smoke')\n"
    info = tarfile.TarInfo("hello.py")
    info.size = len(content)
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.addfile(info, io.BytesIO(content))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def request_json(url: str, token: Optional[str] = None) -> dict:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310 - operator-provided smoke URL
        body = response.read().decode("utf-8")
    return json.loads(body)


def post_json(url: str, payload: dict, token: str, hmac_secret: Optional[str] = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    if hmac_secret:
        ts = str(time.time())
        nonce = str(uuid.uuid4())
        headers["x-omnidesk-sandbox-timestamp"] = ts
        headers["x-omnidesk-sandbox-nonce"] = nonce
        headers["x-omnidesk-sandbox-signature"] = "sha256=" + hmac.new(hmac_secret.encode(), ts.encode() + b"." + nonce.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=max(15, int(payload.get("timeout_seconds", 10)) + 10)) as response:  # nosec B310 - operator-provided smoke URL
        return json.loads(response.read().decode("utf-8"))


def check_app() -> dict:
    base_url = os.getenv("OMNIDESK_SMOKE_BASE_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("OMNIDESK_SMOKE_ADMIN_TOKEN")
    health = request_json(f"{base_url}/health")
    if not health.get("ok"):
        raise RuntimeError(f"health check failed: {health}")
    status = None
    if token:
        status = request_json(f"{base_url}/admin/status", token)
        if not status.get("ok"):
            raise RuntimeError(f"admin status failed: {status}")
    return {"health": health, "status": status}


def check_sandbox() -> dict | None:
    runner_url = os.getenv("OMNIDESK_SMOKE_SANDBOX_URL")
    if not runner_url:
        return None
    runner_url = runner_url.rstrip("/")
    token = os.getenv("OMNIDESK_SMOKE_SANDBOX_TOKEN") or os.getenv("OMNIDESK_SANDBOX_RUNNER_TOKEN")
    if not token:
        raise RuntimeError("OMNIDESK_SMOKE_SANDBOX_TOKEN or OMNIDESK_SANDBOX_RUNNER_TOKEN is required for sandbox smoke")
    health = request_json(f"{runner_url}/health")
    ready = request_json(f"{runner_url}/ready")
    if not health.get("ok") or not ready.get("ok"):
        raise RuntimeError(f"sandbox runner not ready: health={health} ready={ready}")
    payload = {
        "argv": ["python", "-m", "compileall", "."],
        "workspace_archive_base64": build_smoke_workspace_archive(),
        "timeout_seconds": int(os.getenv("OMNIDESK_SMOKE_SANDBOX_TIMEOUT", "30")),
        "readonly": True,
    }
    run = post_json(f"{runner_url}/v1/run", payload, token, os.getenv("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET") or os.getenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET"))
    if not run.get("ok"):
        raise RuntimeError(f"sandbox smoke command failed: {run}")
    return {"health": health, "ready": ready, "run": {"ok": run.get("ok"), "exit_code": run.get("exit_code")}}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(
            "Usage: production_smoke_test.py [--help] [--sandbox-only]\n"
            "\n"
            "Environment variables:\n"
            "  OMNIDESK_SMOKE_BASE_URL              App base URL, default http://127.0.0.1:18789\n"
            "  OMNIDESK_SMOKE_ADMIN_TOKEN           Optional admin bearer token for /admin/status\n"
            "  OMNIDESK_SMOKE_SANDBOX_URL           Optional sandbox runner URL\n"
            "  OMNIDESK_SMOKE_SANDBOX_TOKEN         Sandbox runner bearer token\n"
            "  OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET   Optional sandbox HMAC signing secret\n"
            "  OMNIDESK_SMOKE_SANDBOX_TIMEOUT       Sandbox command timeout seconds\n"
        )
        return 0
    try:
        if "--sandbox-only" in argv:
            sandbox = check_sandbox()
            if sandbox is None:
                raise RuntimeError("OMNIDESK_SMOKE_SANDBOX_URL is required for --sandbox-only")
            result = {"ok": True, "sandbox": sandbox}
        else:
            result = {"ok": True, "app": check_app(), "sandbox": check_sandbox()}
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
