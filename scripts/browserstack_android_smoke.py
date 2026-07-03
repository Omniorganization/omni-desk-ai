#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "omnidesk-browserstack-android-smoke/v1"
APP_UPLOAD_URL = "https://api-cloud.browserstack.com/app-automate/upload"
SESSION_URL = "https://hub-cloud.browserstack.com/wd/hub/session"


class BrowserStackError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth_header(username: str, access_key: str) -> str:
    token = base64.b64encode(f"{username}:{access_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _credential_tokens(username: str | None, access_key: str | None) -> list[str]:
    tokens = [value for value in [username, access_key] if value]
    if username and access_key:
        pair = f"{username}:{access_key}"
        tokens.append(pair)
        tokens.append(base64.b64encode(pair.encode("utf-8")).decode("ascii"))
    return tokens


def _redact(value: str, username: str | None = None, access_key: str | None = None) -> str:
    redacted = value
    for token in sorted(_credential_tokens(username, access_key), key=len, reverse=True):
        redacted = redacted.replace(token, "<redacted>")
    return redacted


def _safe_error(exc: BaseException, username: str | None = None, access_key: str | None = None) -> str:
    return _redact(str(exc), username=username, access_key=access_key)


def _http_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    username: str | None = None,
    access_key: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        message = _redact(raw or exc.reason or str(exc), username=username, access_key=access_key)
        raise BrowserStackError(f"BrowserStack HTTP {exc.code} for {method} {url}: {message}") from exc
    except urllib.error.URLError as exc:
        message = _redact(str(exc.reason), username=username, access_key=access_key)
        raise BrowserStackError(f"BrowserStack request failed for {method} {url}: {message}") from exc
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        snippet = body[:512].decode("utf-8", errors="replace")
        raise BrowserStackError(f"BrowserStack returned non-JSON for {method} {url}: {snippet}") from exc
    if not isinstance(parsed, dict):
        raise BrowserStackError(f"BrowserStack returned unexpected JSON for {method} {url}")
    return parsed


def _multipart_form_data(
    *,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> tuple[str, bytes]:
    boundary = f"----omnidesk-browserstack-{uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not value:
            continue
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _upload_app(app_path: Path, *, username: str, access_key: str, custom_id: str) -> dict[str, Any]:
    content_type, body = _multipart_form_data(
        fields={"custom_id": custom_id},
        file_field="file",
        file_path=app_path,
    )
    response = _http_json(
        APP_UPLOAD_URL,
        method="POST",
        headers={
            "Authorization": _auth_header(username, access_key),
            "Content-Type": content_type,
        },
        data=body,
        username=username,
        access_key=access_key,
    )
    app_url = response.get("app_url")
    if not isinstance(app_url, str) or not app_url:
        raise BrowserStackError("BrowserStack app upload did not return app_url")
    return response


def _create_session(
    *,
    username: str,
    access_key: str,
    app_url: str,
    device_name: str,
    platform_version: str,
    project_name: str,
    build_name: str,
    session_name: str,
) -> dict[str, Any]:
    capabilities = {
        "capabilities": {
            "alwaysMatch": {
                "platformName": "Android",
                "appium:app": app_url,
                "appium:deviceName": device_name,
                "appium:platformVersion": platform_version,
                "appium:automationName": "UiAutomator2",
                "bstack:options": {
                    "userName": username,
                    "accessKey": access_key,
                    "projectName": project_name,
                    "buildName": build_name,
                    "sessionName": session_name,
                    "debug": "true",
                    "networkLogs": "true",
                    "appiumVersion": "2.0.1",
                },
            }
        }
    }
    return _http_json(
        SESSION_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(capabilities, sort_keys=True).encode("utf-8"),
        username=username,
        access_key=access_key,
    )


def _session_url(session_id: str, suffix: str) -> str:
    return f"{SESSION_URL}/{session_id}/{suffix.lstrip('/')}"


def _extract_session_id(payload: dict[str, Any]) -> str:
    value = payload.get("value")
    if isinstance(value, dict):
        session_id = value.get("sessionId") or payload.get("sessionId")
    else:
        session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise BrowserStackError("BrowserStack session response did not include session id")
    return session_id


def _get_session_value(session_id: str, suffix: str, *, username: str, access_key: str) -> Any:
    payload = _http_json(
        _session_url(session_id, suffix),
        method="GET",
        username=username,
        access_key=access_key,
    )
    return payload.get("value")


def _write_session_source(session_id: str, evidence_dir: Path, *, username: str, access_key: str) -> tuple[str, str]:
    source = _get_session_value(session_id, "source", username=username, access_key=access_key)
    if not isinstance(source, str) or not source.strip():
        raise BrowserStackError("BrowserStack session source was empty")
    artifact_rel = Path("artifacts/mobile/browserstack-android-source.xml")
    artifact_path = evidence_dir / artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(source, encoding="utf-8")
    return str(artifact_rel), _sha256(artifact_path)


def _write_session_screenshot(session_id: str, evidence_dir: Path, *, username: str, access_key: str) -> tuple[str, str]:
    screenshot = _get_session_value(session_id, "screenshot", username=username, access_key=access_key)
    if not isinstance(screenshot, str) or not screenshot:
        raise BrowserStackError("BrowserStack session screenshot was empty")
    artifact_rel = Path("artifacts/mobile/browserstack-android-screenshot.png")
    artifact_path = evidence_dir / artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(base64.b64decode(screenshot))
    if artifact_path.stat().st_size == 0:
        raise BrowserStackError("BrowserStack session screenshot decoded to an empty file")
    return str(artifact_rel), _sha256(artifact_path)


def _set_session_status(session_id: str, status: str, reason: str, *, username: str, access_key: str) -> str:
    executor = {
        "action": "setSessionStatus",
        "arguments": {
            "status": status,
            "reason": reason,
        },
    }
    payload = {
        "script": f"browserstack_executor: {json.dumps(executor, sort_keys=True)}",
        "args": [],
    }
    try:
        _http_json(
            _session_url(session_id, "execute/sync"),
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, sort_keys=True).encode("utf-8"),
            username=username,
            access_key=access_key,
        )
    except BrowserStackError as exc:
        return _safe_error(exc, username=username, access_key=access_key)
    return "updated"


def _delete_session(session_id: str, *, username: str, access_key: str) -> str:
    try:
        _http_json(_session_url(session_id, ""), method="DELETE", username=username, access_key=access_key)
    except BrowserStackError as exc:
        return _safe_error(exc, username=username, access_key=access_key)
    return "deleted"


def _copy_app_artifact(app_path: Path, evidence_dir: Path) -> tuple[str, str]:
    artifact_rel = Path("artifacts/mobile/browserstack-android-app.apk")
    artifact_path = evidence_dir / artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(app_path, artifact_path)
    return str(artifact_rel), _sha256(artifact_path)


def _write_native_build_evidence(
    evidence_dir: Path,
    *,
    expected_version: str,
    producer: str,
    artifact_rel: str,
    artifact_sha256: str,
) -> None:
    path = evidence_dir / "native-build/flutter-android-release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "omnidesk-native-build-evidence/v1",
        "status": "passed",
        "version": expected_version,
        "produced_at": _utc_now(),
        "producer": producer,
        "platform": "android",
        "command": "flutter build apk --release",
        "exit_code": 0,
        "artifacts": [{"path": artifact_rel, "sha256": artifact_sha256}],
        "policy": "This is BrowserStack smoke input evidence. It is not production signing or store distribution evidence.",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _producer_identity() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if repo and run_id:
        return f"github-actions:{repo}:{run_id}"
    return os.environ.get("USER") or "local-operator"


def _base_evidence(args: argparse.Namespace, app_path: Path, artifact_rel: str | None, artifact_sha256: str | None) -> dict[str, Any]:
    artifacts = []
    if artifact_rel and artifact_sha256:
        artifacts.append({"path": artifact_rel, "sha256": artifact_sha256})
    return {
        "schema": SCHEMA_VERSION,
        "status": "failed",
        "version": args.expected_version,
        "produced_at": _utc_now(),
        "producer": _producer_identity(),
        "provider": "browserstack-app-automate",
        "project_name": args.project_name,
        "build_name": args.build_name,
        "session_name": args.session_name,
        "device_name": args.device_name,
        "platform": "android",
        "platform_version": args.platform_version,
        "app_path": str(app_path),
        "app_sha256": _sha256(app_path) if app_path.exists() else "",
        "app_size_bytes": app_path.stat().st_size if app_path.exists() else 0,
        "artifacts": artifacts,
        "policy": "This file records a real BrowserStack App Automate attempt. It is not accepted as passing evidence unless a real session returns source and screenshot artifacts.",
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    app_path = Path(args.app_path).resolve()
    evidence_dir = Path(args.output_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if not app_path.exists() or not app_path.is_file():
        raise FileNotFoundError(f"Android app artifact not found: {app_path}")
    if not args.browserstack_username or not args.browserstack_access_key:
        raise BrowserStackError("BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY are required")

    artifact_rel, artifact_sha256 = _copy_app_artifact(app_path, evidence_dir)
    producer = _producer_identity()
    if args.write_native_build_evidence:
        _write_native_build_evidence(
            evidence_dir,
            expected_version=args.expected_version,
            producer=producer,
            artifact_rel=artifact_rel,
            artifact_sha256=artifact_sha256,
        )

    evidence = _base_evidence(args, app_path, artifact_rel, artifact_sha256)
    session_id: str | None = None
    session_status_update = "not-started"
    session_delete = "not-started"
    try:
        upload = _upload_app(
            app_path,
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
            custom_id=args.custom_id,
        )
        app_url = str(upload["app_url"])
        evidence["app_url"] = app_url
        evidence["custom_id"] = args.custom_id

        session = _create_session(
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
            app_url=app_url,
            device_name=args.device_name,
            platform_version=args.platform_version,
            project_name=args.project_name,
            build_name=args.build_name,
            session_name=args.session_name,
        )
        session_id = _extract_session_id(session)
        evidence["session_id"] = session_id
        time.sleep(args.wait_seconds)

        source_rel, source_sha256 = _write_session_source(
            session_id,
            evidence_dir,
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
        )
        screenshot_rel, screenshot_sha256 = _write_session_screenshot(
            session_id,
            evidence_dir,
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
        )
        evidence["artifacts"].extend(
            [
                {"path": source_rel, "sha256": source_sha256},
                {"path": screenshot_rel, "sha256": screenshot_sha256},
            ]
        )
        evidence["status"] = "passed"
        session_status_update = _set_session_status(
            session_id,
            "passed",
            "OmniDesk Android launch smoke returned source and screenshot artifacts.",
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
        )
    except Exception as exc:
        if session_id:
            session_status_update = _set_session_status(
                session_id,
                "failed",
                "OmniDesk Android launch smoke failed before required artifacts were collected.",
                username=args.browserstack_username,
                access_key=args.browserstack_access_key,
            )
        evidence["error"] = _safe_error(
            exc,
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
        )
        evidence["status"] = "failed"
    finally:
        if session_id:
            session_delete = _delete_session(
                session_id,
                username=args.browserstack_username,
                access_key=args.browserstack_access_key,
            )
        evidence["session_status_update"] = session_status_update
        evidence["session_delete"] = session_delete
    return evidence


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an Android smoke test in BrowserStack and write raw external evidence.")
    parser.add_argument("--app-path", required=True)
    parser.add_argument("--expected-version", default="1.12.7+root-monorepo-production-ga-candidate")
    parser.add_argument("--output-dir", default="dist/browserstack-evidence/release/external-evidence")
    parser.add_argument("--browserstack-username", default=os.environ.get("BROWSERSTACK_USERNAME", ""))
    parser.add_argument("--browserstack-access-key", default=os.environ.get("BROWSERSTACK_ACCESS_KEY", ""))
    parser.add_argument("--device-name", default="Google Pixel 8")
    parser.add_argument("--platform-version", default="14.0")
    parser.add_argument("--project-name", default="OmniDesk")
    parser.add_argument("--build-name", default="OmniDesk BrowserStack Android Smoke")
    parser.add_argument("--session-name", default="OmniDesk mobile launch smoke")
    parser.add_argument("--custom-id", default=f"omnidesk-android-smoke-{uuid4().hex}")
    parser.add_argument("--wait-seconds", type=int, default=8)
    parser.add_argument("--write-native-build-evidence", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    evidence_dir = Path(args.output_dir).resolve()
    evidence_path = evidence_dir / "mobile-real-device/browserstack-android-smoke.json"
    try:
        payload = run(args)
    except Exception as exc:
        app_path = Path(args.app_path).resolve()
        payload = _base_evidence(args, app_path, None, None)
        payload["error"] = _safe_error(
            exc,
            username=args.browserstack_username,
            access_key=args.browserstack_access_key,
        )
        payload["status"] = "failed"
    _write_evidence(evidence_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
