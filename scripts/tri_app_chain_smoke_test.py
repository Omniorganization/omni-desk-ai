#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from urllib import error, parse, request

CLIENT_CONFIG = {
    "web-admin": ("OMNIDESK_WEB_ADMIN_BASE_URL", "OMNIDESK_WEB_ADMIN_ADMIN_TOKEN"),
    "desktop": ("OMNIDESK_DESKTOP_API_BASE_URL", "OMNIDESK_DESKTOP_CLIENT_TOKEN"),
    "mobile": ("OMNIDESK_MOBILE_API_BASE_URL", "OMNIDESK_MOBILE_CLIENT_TOKEN"),
}

CHAIN_STEPS = [
    ("session", "OMNIDESK_CHAIN_SESSION_PATH", "POST"),
    ("approval", "OMNIDESK_CHAIN_APPROVAL_PATH", "POST"),
    ("audit", "OMNIDESK_CHAIN_AUDIT_PATH", "GET"),
    ("notification", "OMNIDESK_CHAIN_NOTIFICATION_PATH", "GET"),
]


@dataclass(frozen=True)
class SmokeRequest:
    client: str
    step: str
    method: str
    url: str
    token: str


class SmokeFailure(RuntimeError):
    pass


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def _require_env(name: str) -> str:
    value = _value(name)
    if not value:
        raise SmokeFailure(f"missing required environment value: {name}")
    return value


def _join_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise SmokeFailure(f"invalid smoke path: {path}")
    return base_url.rstrip("/") + path


def build_plan() -> list[SmokeRequest]:
    plan: list[SmokeRequest] = []
    for client, (base_env, token_env) in CLIENT_CONFIG.items():
        base_url = _require_env(base_env)
        token = _require_env(token_env)
        for step, path_env, method in CHAIN_STEPS:
            plan.append(
                SmokeRequest(
                    client=client,
                    step=step,
                    method=method,
                    url=_join_url(base_url, _require_env(path_env)),
                    token=token,
                )
            )
    return plan


def _request_body(smoke_request: SmokeRequest, correlation_id: str, expected_version: str) -> bytes:
    payload = {
        "client": smoke_request.client,
        "correlation_id": correlation_id,
        "expected_version": expected_version,
        "step": smoke_request.step,
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _build_http_request(smoke_request: SmokeRequest, correlation_id: str, expected_version: str) -> request.Request:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {smoke_request.token}",
        "Content-Type": "application/json",
        "X-Omnidesk-Smoke-Client": smoke_request.client,
        "X-Omnidesk-Correlation-Id": correlation_id,
    }
    if smoke_request.method == "GET":
        query = parse.urlencode(
            {
                "client": smoke_request.client,
                "correlation_id": correlation_id,
                "expected_version": expected_version,
                "step": smoke_request.step,
            }
        )
        separator = "&" if "?" in smoke_request.url else "?"
        return request.Request(smoke_request.url + separator + query, headers=headers, method="GET")
    return request.Request(
        smoke_request.url,
        data=_request_body(smoke_request, correlation_id, expected_version),
        headers=headers,
        method=smoke_request.method,
    )


def _decode_json_response(body: bytes) -> dict[str, object] | None:
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def execute_plan(
    plan: list[SmokeRequest],
    *,
    correlation_id: str,
    expected_version: str,
    timeout: float,
    require_correlation_echo: bool,
    opener=request.urlopen,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for smoke_request in plan:
        http_request = _build_http_request(smoke_request, correlation_id, expected_version)
        started = time.monotonic()
        try:
            response = opener(http_request, timeout=timeout)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read()
        except error.HTTPError as exc:
            raise SmokeFailure(f"{smoke_request.client} {smoke_request.step} failed with HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise SmokeFailure(f"{smoke_request.client} {smoke_request.step} failed to connect") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if status < 200 or status >= 300:
            raise SmokeFailure(f"{smoke_request.client} {smoke_request.step} returned HTTP {status}")

        decoded = _decode_json_response(body)
        if decoded and decoded.get("ok") is False:
            raise SmokeFailure(f"{smoke_request.client} {smoke_request.step} returned ok=false")
        if require_correlation_echo and correlation_id not in body.decode("utf-8", errors="ignore"):
            raise SmokeFailure(f"{smoke_request.client} {smoke_request.step} did not echo correlation id")

        results.append(
            {
                "client": smoke_request.client,
                "elapsed_ms": elapsed_ms,
                "method": smoke_request.method,
                "status": status,
                "step": smoke_request.step,
                "url": smoke_request.url,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run tri-app approval/audit/notification/session smoke checks.")
    parser.add_argument("--expected-version", default=os.getenv("EXPECTED_VERSION", ""))
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--correlation-id", default="")
    parser.add_argument("--require-correlation-echo", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    correlation_id = args.correlation_id or f"omni-smoke-{uuid.uuid4().hex}"
    try:
        results = execute_plan(
            build_plan(),
            correlation_id=correlation_id,
            expected_version=args.expected_version,
            timeout=args.timeout,
            require_correlation_echo=args.require_correlation_echo,
        )
    except SmokeFailure as exc:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(exc), "correlation_id": correlation_id}, sort_keys=True), file=sys.stderr)
        else:
            print(f"tri-app chain smoke failed: {exc}", file=sys.stderr)
        return 1

    payload = {"ok": True, "correlation_id": correlation_id, "results": results}
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"tri-app chain smoke ok: {len(results)} checks correlation_id={correlation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
