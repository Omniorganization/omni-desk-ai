#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
import urllib.error
import urllib.request
from typing import Optional


def _archive_base64(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for info, content in members:
            if content is not None:
                info.size = len(content)
            tf.addfile(info, io.BytesIO(content) if content is not None else None)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _file_archive_base64(name: str, content: bytes) -> str:
    return _archive_base64([(tarfile.TarInfo(name), content)])


def _symlink_archive_base64(name: str, linkname: str) -> str:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    return _archive_base64([(info, None)])


def build_smoke_workspace_archive() -> str:
    return _file_archive_base64("hello.py", b"print('omnidesk sandbox smoke')\n")


def request_json(url: str, token: Optional[str] = None) -> dict:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310 - operator-provided smoke URL
        body = response.read().decode("utf-8")
    return json.loads(body)


def _signed_headers(
    body: bytes,
    token: str,
    hmac_secret: Optional[str] = None,
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> dict[str, str]:
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    if hmac_secret:
        ts = timestamp or str(time.time())
        nonce = nonce or str(uuid.uuid4())
        headers["x-omnidesk-sandbox-timestamp"] = ts
        headers["x-omnidesk-sandbox-nonce"] = nonce
        headers["x-omnidesk-sandbox-signature"] = "sha256=" + hmac.new(hmac_secret.encode(), ts.encode() + b"." + nonce.encode() + b"." + body, hashlib.sha256).hexdigest()
    return headers


def _decode_error_body(exc: urllib.error.HTTPError) -> dict:
    raw = exc.read().decode("utf-8", errors="replace")
    if not raw:
        return {"ok": False, "error": str(exc)}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": raw}


def post_json_response(
    url: str,
    payload: dict,
    token: str,
    hmac_secret: Optional[str] = None,
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _signed_headers(body, token, hmac_secret, nonce=nonce, timestamp=timestamp)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(15, int(payload.get("timeout_seconds", 10)) + 10)) as response:  # nosec B310 - operator-provided smoke URL
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            return status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), _decode_error_body(exc)


def post_json(url: str, payload: dict, token: str, hmac_secret: Optional[str] = None) -> dict:
    status, data = post_json_response(url, payload, token, hmac_secret)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {data}")
    return data


def _assert_rejected(label: str, status: int, data: dict, expected_statuses: set[int], expected_text: str) -> dict:
    text = json.dumps(data, ensure_ascii=False)
    if status not in expected_statuses or data.get("ok") is not False or expected_text not in text:
        raise RuntimeError(f"strict sandbox {label} check failed: status={status} body={data}")
    return {"status": status, "ok": data.get("ok"), "matched": expected_text}


def check_strict_sandbox(runner_url: str, token: str, hmac_secret: str, timeout_seconds: int) -> dict:
    run_url = f"{runner_url}/v1/run"
    base_payload = {
        "argv": ["python", "-m", "compileall", "."],
        "timeout_seconds": timeout_seconds,
        "readonly": True,
    }
    checks: dict[str, dict] = {}

    traversal = dict(base_payload, workspace_archive_base64=_file_archive_base64("../evil.py", b"print('evil')\n"))
    status, data = post_json_response(run_url, traversal, token, hmac_secret)
    checks["path_traversal_archive"] = _assert_rejected("path traversal archive", status, data, {400, 500}, "unsafe path")

    symlink = dict(base_payload, workspace_archive_base64=_symlink_archive_base64("escape", "/etc/passwd"))
    status, data = post_json_response(run_url, symlink, token, hmac_secret)
    checks["symlink_escape_archive"] = _assert_rejected("symlink archive", status, data, {400, 500}, "links")

    blocked = dict(base_payload, argv=["bash", "-c", "id"], workspace_archive_base64=build_smoke_workspace_archive())
    status, data = post_json_response(run_url, blocked, token, hmac_secret)
    checks["command_allowlist_reject"] = _assert_rejected("command allowlist", status, data, {400}, "command blocked")

    nonce = str(uuid.uuid4())
    timestamp = str(time.time())
    status, data = post_json_response(run_url, blocked, token, hmac_secret, nonce=nonce, timestamp=timestamp)
    _assert_rejected("replay seed", status, data, {400}, "command blocked")
    status, data = post_json_response(run_url, blocked, token, hmac_secret, nonce=nonce, timestamp=timestamp)
    checks["nonce_replay_reject"] = _assert_rejected("nonce replay", status, data, {401}, "replayed signature nonce")

    oversized = dict(base_payload, workspace_archive_base64=_file_archive_base64("large.py", b"x" * (1024 * 1024 + 1)))
    status, data = post_json_response(run_url, oversized, token, hmac_secret)
    checks["oversized_archive_reject"] = _assert_rejected("oversized archive", status, data, {413, 500}, "exceeds maximum size")
    return checks


def _slo_is_healthy(payload: dict) -> bool:
    evaluations = payload.get("slo") or payload.get("evaluations") or []
    if isinstance(evaluations, dict):
        evaluations = evaluations.get("checks") or evaluations.get("items") or []
    if not isinstance(evaluations, list):
        return True
    for item in evaluations:
        if isinstance(item, dict) and item.get("ok") is False:
            return False
        if isinstance(item, dict) and str(item.get("status", "")).lower() in {"fail", "failed", "breached", "critical"}:
            return False
    return True


def _assert_expected_runtime_identity(payloads: list[dict | None], *, expected_version: str | None = None, expected_artifact_sha256: str | None = None, expected_build_sha: str | None = None, expected_image_digest: str | None = None) -> dict:
    observed: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = [payload]
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            candidates.append(runtime)
        for candidate in candidates:
            for key in ("version", "artifact_sha256", "build_sha", "image_digest"):
                value = candidate.get(key)
                if value and key not in observed:
                    observed[key] = str(value)
    expectations = {
        "version": expected_version,
        "artifact_sha256": expected_artifact_sha256,
        "build_sha": expected_build_sha,
        "image_digest": expected_image_digest,
    }
    for key, expected in expectations.items():
        if expected and observed.get(key) != expected:
            raise RuntimeError(f"runtime {key} mismatch: expected {expected}, observed {observed.get(key)}")
    return observed


def check_app(*, check_admin_metrics: bool = False, check_admin_slo: bool = False, fail_on_slo: bool = False, expected_version: str | None = None, expected_artifact_sha256: str | None = None, expected_build_sha: str | None = None, expected_image_digest: str | None = None) -> dict:
    base_url = os.getenv("OMNIDESK_SMOKE_BASE_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("OMNIDESK_SMOKE_ADMIN_TOKEN")
    health = request_json(f"{base_url}/health")
    if not health.get("ok"):
        raise RuntimeError(f"health check failed: {health}")
    status = None
    metrics = None
    slo = None
    if token:
        status = request_json(f"{base_url}/admin/status", token)
        if not status.get("ok"):
            raise RuntimeError(f"admin status failed: {status}")
        if check_admin_metrics:
            # /admin/metrics returns Prometheus text. A successful HTTP response
            # is enough for smoke; request_json is intentionally not used.
            req = urllib.request.Request(f"{base_url}/admin/metrics", headers={"authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310 - operator-provided smoke URL
                text = response.read().decode("utf-8", errors="replace")
            metrics = {"checked": True, "bytes": len(text)}
        if check_admin_slo or fail_on_slo:
            slo = request_json(f"{base_url}/admin/slo", token)
            if fail_on_slo and not _slo_is_healthy(slo):
                raise RuntimeError(f"SLO gate failed: {slo}")
    elif check_admin_metrics or check_admin_slo or fail_on_slo:
        raise RuntimeError("OMNIDESK_SMOKE_ADMIN_TOKEN is required for admin metrics/SLO smoke checks")
    runtime_identity = _assert_expected_runtime_identity(
        [health, status],
        expected_version=expected_version,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_build_sha=expected_build_sha,
        expected_image_digest=expected_image_digest,
    )
    return {"health": health, "status": status, "metrics": metrics, "slo": slo, "runtime_identity": runtime_identity}


def check_sandbox(*, strict: bool = False) -> dict | None:
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
    hmac_secret = os.getenv("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET") or os.getenv("OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET")
    run = post_json(f"{runner_url}/v1/run", payload, token, hmac_secret)
    if not run.get("ok"):
        raise RuntimeError(f"sandbox smoke command failed: {run}")
    result = {"health": health, "ready": ready, "run": {"ok": run.get("ok"), "exit_code": run.get("exit_code")}}
    if strict:
        if not hmac_secret:
            raise RuntimeError("OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET or OMNIDESK_SANDBOX_RUNNER_HMAC_SECRET is required for --strict-sandbox")
        result["strict_sandbox"] = check_strict_sandbox(runner_url, token, hmac_secret, int(os.getenv("OMNIDESK_SMOKE_SANDBOX_TIMEOUT", "30")))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OmniDesk production smoke checks.")
    parser.add_argument("--sandbox-only", action="store_true", help="Only check the sandbox runner.")
    parser.add_argument("--strict-sandbox", action="store_true", help="Exercise negative sandbox security checks.")
    parser.add_argument("--base-url", help="Application base URL. Overrides OMNIDESK_SMOKE_BASE_URL.")
    parser.add_argument("--admin-token-env", default="OMNIDESK_SMOKE_ADMIN_TOKEN", help="Environment variable containing the app admin token.")
    parser.add_argument("--sandbox-url", help="Sandbox runner URL. Overrides OMNIDESK_SMOKE_SANDBOX_URL.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output. This is the default output format.")
    parser.add_argument("--fail-on-slo", action="store_true", help="Fail when /admin/slo reports a breached SLO.")
    parser.add_argument("--check-admin-metrics", action="store_true", help="Require /admin/metrics to return successfully.")
    parser.add_argument("--check-admin-slo", action="store_true", help="Require /admin/slo to return successfully.")
    parser.add_argument("--expected-version", help="Require /health or /admin/status to report this version.")
    parser.add_argument("--expected-artifact-sha256", help="Require runtime identity to report this artifact SHA-256.")
    parser.add_argument("--expected-build-sha", help="Require runtime identity to report this build SHA.")
    parser.add_argument("--expected-image-digest", help="Require runtime identity to report this OCI image digest.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.base_url:
        os.environ["OMNIDESK_SMOKE_BASE_URL"] = args.base_url
    if args.sandbox_url:
        os.environ["OMNIDESK_SMOKE_SANDBOX_URL"] = args.sandbox_url
    if args.admin_token_env and args.admin_token_env != "OMNIDESK_SMOKE_ADMIN_TOKEN":
        token = os.getenv(args.admin_token_env)
        if token:
            os.environ["OMNIDESK_SMOKE_ADMIN_TOKEN"] = token
    try:
        if args.sandbox_only:
            sandbox = check_sandbox(strict=True) if args.strict_sandbox else check_sandbox()
            if sandbox is None:
                raise RuntimeError("OMNIDESK_SMOKE_SANDBOX_URL is required for --sandbox-only")
            result = {"ok": True, "sandbox": sandbox}
        else:
            result = {
                "ok": True,
                "app": check_app(
                    check_admin_metrics=args.check_admin_metrics,
                    check_admin_slo=args.check_admin_slo,
                    fail_on_slo=args.fail_on_slo,
                    expected_version=args.expected_version,
                    expected_artifact_sha256=args.expected_artifact_sha256,
                    expected_build_sha=args.expected_build_sha,
                    expected_image_digest=args.expected_image_digest,
                ),
                "sandbox": check_sandbox(strict=True) if args.strict_sandbox else check_sandbox(),
            }
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
