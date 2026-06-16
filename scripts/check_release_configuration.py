#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import urlparse

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REF_WITH_DIGEST_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
APPLE_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
SAFE_SYSTEMD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.@:/+=-]+$")

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
    | {name for names in DEPLOY_MODE_VARS.values() for name in names}
    | set(OPTIONAL_DEPLOY_VARS)
)


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_missing(name: str) -> bool:
    return _value(name) == ""


def _require(names: list[str], label: str, issues: list[str]) -> None:
    for name in names:
        raw = os.getenv(name, "")
        if raw.strip() == "":
            issues.append(f"missing {label}: {name}")
        elif raw != raw.strip():
            issues.append(f"invalid {label}: {name} must not have leading or trailing whitespace")


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_release_shapes(issues: list[str]) -> None:
    digest = _value("OMNIDESK_SANDBOX_RUNNER_DIGEST")
    if digest and not DIGEST_RE.fullmatch(digest):
        issues.append("invalid var: OMNIDESK_SANDBOX_RUNNER_DIGEST must match sha256:<64 lowercase hex chars>")
    team_id = _value("OMNI_IOS_APPLE_TEAM_ID")
    if team_id and not APPLE_TEAM_ID_RE.fullmatch(team_id):
        issues.append("invalid var: OMNI_IOS_APPLE_TEAM_ID must be a 10-character Apple team id")


def _validate_smoke_shapes(issues: list[str], *, require_sandbox_smoke: bool) -> None:
    for name in SMOKE_VARS + (SANDBOX_SMOKE_VARS if require_sandbox_smoke else []):
        value = _value(name)
        if value and not _is_http_url(value):
            issues.append(f"invalid var: {name} must be an http(s) URL with a host")


def _validate_deploy_shapes(_scope: str, deploy_mode: str, issues: list[str]) -> None:
    image = _value("OMNIDESK_IMAGE")
    if deploy_mode == "kubectl" and image and not IMAGE_REF_WITH_DIGEST_RE.fullmatch(image):
        issues.append("invalid var: kubectl OMNIDESK_IMAGE must be pinned by digest")
    if deploy_mode != "systemd":
        return
    systemd_values = {
        "OMNIDESK_DEPLOY_HOST": _value("OMNIDESK_DEPLOY_HOST"),
        "OMNIDESK_DEPLOY_USER": _value("OMNIDESK_DEPLOY_USER"),
        "OMNIDESK_REMOTE_DEPLOY_SCRIPT": _value("OMNIDESK_REMOTE_DEPLOY_SCRIPT"),
    }
    for name, value in systemd_values.items():
        if value and not SAFE_SYSTEMD_TOKEN_RE.fullmatch(value):
            issues.append(f"invalid var: {name} contains unsupported characters")
    remote_script = systemd_values["OMNIDESK_REMOTE_DEPLOY_SCRIPT"]
    if remote_script and not remote_script.startswith("/usr/local/bin/"):
        issues.append("invalid var: OMNIDESK_REMOTE_DEPLOY_SCRIPT must be under /usr/local/bin")


def check_release() -> list[str]:
    issues: list[str] = []
    _require(RELEASE_SECRETS, "secret", issues)
    _require(RELEASE_VARS, "var", issues)
    _validate_release_shapes(issues)
    return issues


def check_downstream(scope: str, deploy_mode: str, *, require_sandbox_smoke: bool) -> list[str]:
    issues: list[str] = []
    if scope == "production" and deploy_mode == "noop":
        issues.append("invalid deploy mode: production promotion must not use noop")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail fast when release or downstream GitHub configuration is incomplete.")
    parser.add_argument("--scope", choices=["release", "staging", "production", "rollback"], required=True)
    parser.add_argument("--deploy-mode", choices=sorted(DEPLOY_MODE_VARS), default="noop")
    parser.add_argument("--require-sandbox-smoke", action="store_true")
    args = parser.parse_args(argv)

    if args.scope == "release":
        issues = check_release()
    else:
        issues = check_downstream(args.scope, args.deploy_mode, require_sandbox_smoke=args.require_sandbox_smoke)

    if issues:
        print(f"{args.scope} configuration preflight failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"{args.scope} configuration preflight ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
