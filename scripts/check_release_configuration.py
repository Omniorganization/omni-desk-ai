#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
URL_RE = re.compile(r"^https?://")

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
        "OMNIDESK_REMOTE_DEPLOY_SCRIPT",
    ],
}

ALL_KNOWN_NAMES = sorted(
    set(RELEASE_SECRETS)
    | set(RELEASE_VARS)
    | set(ARTIFACT_VERIFY_SECRETS)
    | set(SMOKE_SECRETS)
    | set(SANDBOX_SMOKE_SECRETS)
    | set(SMOKE_VARS)
    | set(SANDBOX_SMOKE_VARS)
    | {name for names in DEPLOY_MODE_VARS.values() for name in names}
)


def _is_missing(name: str) -> bool:
    return os.getenv(name, "") == ""


def _require(names: list[str], label: str, issues: list[str]) -> None:
    for name in names:
        if _is_missing(name):
            issues.append(f"missing {label}: {name}")


def _validate_release_shapes(issues: list[str]) -> None:
    digest = os.getenv("OMNIDESK_SANDBOX_RUNNER_DIGEST", "")
    if digest and not DIGEST_RE.fullmatch(digest):
        issues.append("invalid var: OMNIDESK_SANDBOX_RUNNER_DIGEST must match sha256:<64 lowercase hex chars>")


def _validate_smoke_shapes(issues: list[str], *, require_sandbox_smoke: bool) -> None:
    for name in SMOKE_VARS + (SANDBOX_SMOKE_VARS if require_sandbox_smoke else []):
        value = os.getenv(name, "")
        if value and not URL_RE.match(value):
            issues.append(f"invalid var: {name} must be an http(s) URL")


def _validate_deploy_shapes(scope: str, deploy_mode: str, issues: list[str]) -> None:
    image = os.getenv("OMNIDESK_IMAGE", "")
    if scope == "production" and deploy_mode == "kubectl" and image and "@sha256:" not in image:
        issues.append("invalid var: production kubectl OMNIDESK_IMAGE must be pinned by digest")
    remote_script = os.getenv("OMNIDESK_REMOTE_DEPLOY_SCRIPT", "")
    if deploy_mode == "systemd" and remote_script and not remote_script.startswith("/usr/local/bin/"):
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
