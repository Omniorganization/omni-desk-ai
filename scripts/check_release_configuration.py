#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APPLE_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
SAFE_SYSTEMD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.@:/+=-]+$")
# Pragmatic Docker/OCI image reference validator for the name/tag portion before @sha256:.
# It rejects whitespace, URL schemes, empty path components, uppercase repository names,
# multiple @ separators, and shell-looking values while still allowing registry ports and tags.
IMAGE_NAME_RE = re.compile(
    r"^(?=.{1,255}$)"
    r"[a-z0-9]+(?:(?:[._-]|__)[a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:(?:[._-]|__)[a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?)*$"
)

RELEASE_SECRETS = [
    "OMNI_ANDROID_KEYSTORE_BASE64",
    "OMNI_ANDROID_KEYSTORE_PASSWORD",
    "OMNI_ANDROID_KEY_ALIAS",
    "OMNI_ANDROID_KEY_PASSWORD",
    "OMNI_ANDROID_GOOGLE_SERVICES_JSON",
    "OMNI_IOS_CERTIFICATE_P12_BASE64",
    "OMNI_IOS_CERTIFICATE_PASSWORD",
    "OMNI_IOS_PROVISIONING_PROFILE_BASE64",
    "OMNI_IOS_KEYCHAIN_PASSWORD",
    "OMNIDESK_RELEASE_SIGNING_KEY",
]

RELEASE_VARS = [
    "OMNI_IOS_APPLE_TEAM_ID",
    "OMNIDESK_SANDBOX_RUNNER_DIGEST",
]

ARTIFACT_VERIFY_SECRETS = [
    "OMNIDESK_RELEASE_SIGNING_KEY",
]

SMOKE_SECRETS = [
    "OMNIDESK_SMOKE_ADMIN_TOKEN",
]

SANDBOX_SMOKE_SECRETS = [
    "OMNIDESK_SMOKE_SANDBOX_TOKEN",
    "OMNIDESK_SMOKE_SANDBOX_HMAC_SECRET",
]

SMOKE_VARS = [
    "OMNIDESK_SMOKE_BASE_URL",
]

SANDBOX_SMOKE_VARS = [
    "OMNIDESK_SMOKE_SANDBOX_URL",
]

WEB_ADMIN_SECRETS = [
    "OMNIDESK_WEB_ADMIN_ADMIN_TOKEN",
]

WEB_ADMIN_URL_VARS = [
    "OMNIDESK_WEB_ADMIN_BASE_URL",
    "OMNIDESK_WEB_ADMIN_API_BASE_URL",
]

WEB_ADMIN_PATH_VARS = [
    "OMNIDESK_WEB_ADMIN_SESSION_PATH",
    "OMNIDESK_WEB_ADMIN_APPROVAL_PATH",
    "OMNIDESK_WEB_ADMIN_AUDIT_PATH",
    "OMNIDESK_WEB_ADMIN_NOTIFICATION_PATH",
]

DESKTOP_SECRETS = [
    "OMNIDESK_DESKTOP_CLIENT_TOKEN",
]

DESKTOP_URL_VARS = [
    "OMNIDESK_DESKTOP_API_BASE_URL",
]

DESKTOP_PATH_VARS = [
    "OMNIDESK_DESKTOP_SESSION_PATH",
    "OMNIDESK_DESKTOP_APPROVAL_PATH",
    "OMNIDESK_DESKTOP_AUDIT_PATH",
    "OMNIDESK_DESKTOP_NOTIFICATION_PATH",
]

DESKTOP_PROFILE_VARS = [
    "OMNIDESK_DESKTOP_UPDATE_CHANNEL",
]

MOBILE_SECRETS = [
    "OMNIDESK_MOBILE_CLIENT_TOKEN",
]

MOBILE_URL_VARS = [
    "OMNIDESK_MOBILE_API_BASE_URL",
]

TRI_APP_CHAIN_PATH_VARS = [
    "OMNIDESK_CHAIN_SESSION_PATH",
    "OMNIDESK_CHAIN_APPROVAL_PATH",
    "OMNIDESK_CHAIN_AUDIT_PATH",
    "OMNIDESK_CHAIN_NOTIFICATION_PATH",
]

DEPLOY_MODE_VARS = {
    "noop": [],
    "docker-compose": [
        "OMNIDESK_DEPLOY_COMPOSE_FILE",
        "OMNIDESK_DEPLOY_SERVICE",
    ],
    "kubectl": [
        "OMNIDESK_DEPLOY_KUBE_CONTEXT",
        "OMNIDESK_DEPLOY_NAMESPACE",
        "OMNIDESK_DEPLOYMENT_NAME",
        "OMNIDESK_CONTAINER_NAME",
        "OMNIDESK_IMAGE",
    ],
    "systemd": [
        "OMNIDESK_DEPLOY_HOST",
        "OMNIDESK_DEPLOY_USER",
    ],
}

OPTIONAL_DEPLOY_VARS = [
    "OMNIDESK_REMOTE_DEPLOY_SCRIPT",
]

ALL_KNOWN_NAMES = sorted(
    set(RELEASE_SECRETS)
    | set(RELEASE_VARS)
    | set(ARTIFACT_VERIFY_SECRETS)
    | set(SMOKE_SECRETS)
    | set(SANDBOX_SMOKE_SECRETS)
    | set(SMOKE_VARS)
    | set(SANDBOX_SMOKE_VARS)
    | set(WEB_ADMIN_SECRETS)
    | set(WEB_ADMIN_URL_VARS)
    | set(WEB_ADMIN_PATH_VARS)
    | set(DESKTOP_SECRETS)
    | set(DESKTOP_URL_VARS)
    | set(DESKTOP_PATH_VARS)
    | set(DESKTOP_PROFILE_VARS)
    | set(MOBILE_SECRETS)
    | set(MOBILE_URL_VARS)
    | set(TRI_APP_CHAIN_PATH_VARS)
    | {name for names in DEPLOY_MODE_VARS.values() for name in names}
    | set(OPTIONAL_DEPLOY_VARS)
)

DESKTOP_UPDATE_CHANNELS = {"stable", "beta", "internal", "nightly"}


@dataclass(frozen=True)
class Issue:
    severity: str
    kind: str
    name: str
    message: str

    def __str__(self) -> str:
        return self.message


def _issue(issues: list[Issue], *, severity: str, kind: str, name: str, message: str) -> None:
    issues.append(Issue(severity=severity, kind=kind, name=name, message=message))


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_missing(name: str) -> bool:
    return _value(name) == ""


def _require(names: list[str], label: str, issues: list[Issue]) -> None:
    for name in names:
        raw = os.getenv(name, "")
        if raw.strip() == "":
            _issue(
                issues,
                severity="blocker",
                kind=f"missing_{label}",
                name=name,
                message=f"missing {label}: {name}",
            )
        elif raw != raw.strip():
            _issue(
                issues,
                severity="blocker",
                kind=f"invalid_{label}",
                name=name,
                message=f"invalid {label}: {name} must not have leading or trailing whitespace",
            )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_canonical_http_path(value: str) -> bool:
    if not value.startswith("/") or value.startswith("//"):
        return False
    if any(ch.isspace() for ch in value):
        return False
    normalized = posixpath.normpath(value)
    return normalized == value and ".." not in Path(value).parts and value != "/"


def _is_digest_pinned_image_ref(value: str) -> bool:
    if value != value.strip() or not value or any(ch.isspace() for ch in value):
        return False
    if "://" in value or value.count("@") != 1:
        return False
    image_name, digest = value.split("@", 1)
    if not image_name or not DIGEST_RE.fullmatch(digest):
        return False
    if "//" in image_name or "/./" in image_name or "/../" in image_name:
        return False
    return bool(IMAGE_NAME_RE.fullmatch(image_name))


def _validate_release_shapes(issues: list[Issue]) -> None:
    digest = _value("OMNIDESK_SANDBOX_RUNNER_DIGEST")
    if digest and not DIGEST_RE.fullmatch(digest):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_SANDBOX_RUNNER_DIGEST",
            message="invalid var: OMNIDESK_SANDBOX_RUNNER_DIGEST must match sha256:<64 lowercase hex chars>",
        )
    team_id = _value("OMNI_IOS_APPLE_TEAM_ID")
    if team_id and not APPLE_TEAM_ID_RE.fullmatch(team_id):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNI_IOS_APPLE_TEAM_ID",
            message="invalid var: OMNI_IOS_APPLE_TEAM_ID must be a 10-character Apple team id",
        )


def _validate_smoke_shapes(issues: list[Issue], *, require_sandbox_smoke: bool) -> None:
    for name in SMOKE_VARS + (SANDBOX_SMOKE_VARS if require_sandbox_smoke else []):
        value = _value(name)
        if value and not _is_http_url(value):
            _issue(
                issues,
                severity="blocker",
                kind="invalid_var",
                name=name,
                message=f"invalid var: {name} must be an http(s) URL with a host",
            )


def _validate_url_vars(names: list[str], issues: list[Issue]) -> None:
    for name in names:
        value = _value(name)
        if value and not _is_http_url(value):
            _issue(
                issues,
                severity="blocker",
                kind="invalid_var",
                name=name,
                message=f"invalid var: {name} must be an http(s) URL with a host",
            )


def _validate_path_vars(names: list[str], issues: list[Issue]) -> None:
    for name in names:
        value = _value(name)
        if value and not _is_canonical_http_path(value):
            _issue(
                issues,
                severity="blocker",
                kind="invalid_var",
                name=name,
                message=f"invalid var: {name} must be an absolute canonical HTTP path",
            )


def _validate_desktop_profile(issues: list[Issue]) -> None:
    channel = _value("OMNIDESK_DESKTOP_UPDATE_CHANNEL")
    if channel and channel not in DESKTOP_UPDATE_CHANNELS:
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_DESKTOP_UPDATE_CHANNEL",
            message="invalid var: OMNIDESK_DESKTOP_UPDATE_CHANNEL must be stable, beta, internal, or nightly",
        )


def _validate_systemd_remote_script(value: str, issues: list[Issue]) -> None:
    if not value:
        return
    if not SAFE_SYSTEMD_TOKEN_RE.fullmatch(value):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_REMOTE_DEPLOY_SCRIPT",
            message="invalid var: OMNIDESK_REMOTE_DEPLOY_SCRIPT contains unsupported characters",
        )
        return
    if not value.startswith("/"):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_REMOTE_DEPLOY_SCRIPT",
            message="invalid var: OMNIDESK_REMOTE_DEPLOY_SCRIPT must be an absolute path under /usr/local/bin",
        )
        return
    normalized = posixpath.normpath(value)
    if normalized != value or ".." in Path(value).parts:
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_REMOTE_DEPLOY_SCRIPT",
            message="invalid var: OMNIDESK_REMOTE_DEPLOY_SCRIPT must be a canonical path without . or .. segments",
        )
        return
    if not normalized.startswith("/usr/local/bin/") or normalized == "/usr/local/bin/":
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_REMOTE_DEPLOY_SCRIPT",
            message="invalid var: OMNIDESK_REMOTE_DEPLOY_SCRIPT must be under /usr/local/bin",
        )


def _validate_deploy_shapes(_scope: str, deploy_mode: str, issues: list[Issue]) -> None:
    image = _value("OMNIDESK_IMAGE")
    if deploy_mode == "kubectl" and image and not _is_digest_pinned_image_ref(image):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNIDESK_IMAGE",
            message="invalid var: kubectl OMNIDESK_IMAGE must be pinned by digest",
        )
    if deploy_mode != "systemd":
        return
    systemd_values = {
        "OMNIDESK_DEPLOY_HOST": _value("OMNIDESK_DEPLOY_HOST"),
        "OMNIDESK_DEPLOY_USER": _value("OMNIDESK_DEPLOY_USER"),
    }
    for name, value in systemd_values.items():
        if value and not SAFE_SYSTEMD_TOKEN_RE.fullmatch(value):
            _issue(
                issues,
                severity="blocker",
                kind="invalid_var",
                name=name,
                message=f"invalid var: {name} contains unsupported characters",
            )
    _validate_systemd_remote_script(_value("OMNIDESK_REMOTE_DEPLOY_SCRIPT"), issues)


def check_release() -> list[Issue]:
    issues: list[Issue] = []
    _require(RELEASE_SECRETS, "secret", issues)
    _require(RELEASE_VARS, "var", issues)
    _validate_release_shapes(issues)
    return issues


def check_downstream(scope: str, deploy_mode: str, *, require_sandbox_smoke: bool) -> list[Issue]:
    issues: list[Issue] = []
    if scope == "production" and deploy_mode == "noop":
        _issue(
            issues,
            severity="blocker",
            kind="invalid_deploy_mode",
            name="deploy_mode",
            message="invalid deploy mode: production promotion must not use noop",
        )
    _require(ARTIFACT_VERIFY_SECRETS, "secret", issues)
    _require(SMOKE_SECRETS, "secret", issues)
    _require(SMOKE_VARS, "var", issues)
    if require_sandbox_smoke:
        _require(SANDBOX_SMOKE_SECRETS, "secret", issues)
        _require(SANDBOX_SMOKE_VARS, "var", issues)
    _require(DEPLOY_MODE_VARS[deploy_mode], "var", issues)
    _validate_smoke_shapes(issues, require_sandbox_smoke=require_sandbox_smoke)
    _validate_deploy_shapes(scope, deploy_mode, issues)
    return issues


def check_web_admin() -> list[Issue]:
    issues: list[Issue] = []
    _require(WEB_ADMIN_SECRETS, "secret", issues)
    _require(WEB_ADMIN_URL_VARS, "var", issues)
    _require(WEB_ADMIN_PATH_VARS, "var", issues)
    _validate_url_vars(WEB_ADMIN_URL_VARS, issues)
    _validate_path_vars(WEB_ADMIN_PATH_VARS, issues)
    return issues


def check_desktop() -> list[Issue]:
    issues: list[Issue] = []
    _require(DESKTOP_SECRETS, "secret", issues)
    _require(DESKTOP_URL_VARS, "var", issues)
    _require(DESKTOP_PATH_VARS, "var", issues)
    _require(DESKTOP_PROFILE_VARS, "var", issues)
    _validate_url_vars(DESKTOP_URL_VARS, issues)
    _validate_path_vars(DESKTOP_PATH_VARS, issues)
    _validate_desktop_profile(issues)
    return issues


def check_tri_app_smoke() -> list[Issue]:
    issues: list[Issue] = []
    _require(WEB_ADMIN_SECRETS + DESKTOP_SECRETS + MOBILE_SECRETS, "secret", issues)
    _require(WEB_ADMIN_URL_VARS[:1] + DESKTOP_URL_VARS + MOBILE_URL_VARS, "var", issues)
    _require(TRI_APP_CHAIN_PATH_VARS, "var", issues)
    _validate_url_vars(WEB_ADMIN_URL_VARS[:1] + DESKTOP_URL_VARS + MOBILE_URL_VARS, issues)
    _validate_path_vars(TRI_APP_CHAIN_PATH_VARS, issues)
    return issues


def _result_payload(scope: str, deploy_mode: str, issues: list[Issue]) -> dict[str, object]:
    return {
        "scope": scope,
        "deploy_mode": deploy_mode,
        "ok": not issues,
        "issue_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def _write_report(report_path: str | None, payload: dict[str, object]) -> None:
    if not report_path:
        return
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _emit_text(scope: str, issues: list[Issue]) -> None:
    if issues:
        print(f"{scope} configuration preflight failed:", file=sys.stderr)
        for issue in issues:
            print(f"- [{issue.severity}] {issue.message}", file=sys.stderr)
    else:
        print(f"{scope} configuration preflight ok")


def _emit_json(payload: dict[str, object], *, stream) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail fast when release or downstream GitHub configuration is incomplete.")
    parser.add_argument(
        "--scope",
        choices=["release", "staging", "production", "rollback", "web-admin", "desktop", "tri-app-smoke"],
        required=True,
    )
    parser.add_argument("--deploy-mode", choices=sorted(DEPLOY_MODE_VARS), default="noop")
    parser.add_argument("--require-sandbox-smoke", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--report-path", help="Write a JSON preflight report to this path.")
    args = parser.parse_args(argv)

    if args.scope == "release":
        issues = check_release()
    elif args.scope in {"staging", "production", "rollback"}:
        issues = check_downstream(args.scope, args.deploy_mode, require_sandbox_smoke=args.require_sandbox_smoke)
    elif args.scope == "web-admin":
        issues = check_web_admin()
    elif args.scope == "desktop":
        issues = check_desktop()
    else:
        issues = check_tri_app_smoke()

    payload = _result_payload(args.scope, args.deploy_mode, issues)
    _write_report(args.report_path, payload)

    if args.format == "json":
        _emit_json(payload, stream=sys.stderr if issues else sys.stdout)
    else:
        _emit_text(args.scope, issues)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
