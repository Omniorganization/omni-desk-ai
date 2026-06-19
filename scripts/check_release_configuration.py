#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APPLE_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
SAFE_SYSTEMD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.@:/+=-]+$")
ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
REVERSE_DNS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+$")
ANDROID_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
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

WEB_ADMIN_SECRETS = [
    "WEB_ADMIN_ADMIN_TOKEN",
    "WEB_ADMIN_AUTH_SECRET",
]

WEB_ADMIN_VARS = [
    "WEB_ADMIN_BASE_URL",
    "WEB_ADMIN_API_BASE_URL",
    "WEB_ADMIN_IMAGE",
]

DESKTOP_SECRETS = [
    "DESKTOP_BRIDGE_TOKEN",
    "DESKTOP_BRIDGE_HMAC_SECRET",
]

DESKTOP_VARS = [
    "DESKTOP_AGENT_BASE_URL",
    "DESKTOP_UPDATE_ENDPOINT",
    "DESKTOP_APP_IDENTIFIER",
    "DESKTOP_BRIDGE_ORIGIN",
]

MOBILE_SECRETS = [
    "MOBILE_APPROVAL_TOKEN",
    "MOBILE_PUSH_HMAC_SECRET",
]

MOBILE_VARS = [
    "MOBILE_API_BASE_URL",
    "MOBILE_APPROVAL_CALLBACK_URL",
    "OMNI_ANDROID_PACKAGE_NAME",
    "OMNI_IOS_BUNDLE_ID",
]

TRI_APP_SECRETS = [
    "TRI_APP_ADMIN_TOKEN",
    "TRI_APP_MOBILE_APPROVAL_TOKEN",
    "TRI_APP_DESKTOP_AGENT_TOKEN",
    "TRI_APP_AUDIT_HMAC_SECRET",
]

TRI_APP_VARS = [
    "TRI_APP_BACKEND_BASE_URL",
    "TRI_APP_WEB_ADMIN_BASE_URL",
    "TRI_APP_MOBILE_CALLBACK_URL",
    "TRI_APP_DESKTOP_AGENT_URL",
    "TRI_APP_ORG_ID",
]

IOS_EVIDENCE_VARS = [
    "IOS_EVIDENCE_RAW_DIR",
    "IOS_EVIDENCE_EXPECTED_VERSION",
]

IOS_EVIDENCE_REQUIRED_RELATIVE_FILES = [
    "native-build/flutter-ios-release.json",
    "signed-artifacts/ios-signed-ipa.json",
    "push/apns-live-delivery.json",
]

MOBILE_REAL_DEVICE_SECRETS = [
    "MOBILE_APPROVAL_TOKEN",
]

MOBILE_REAL_DEVICE_VARS = [
    "IOS_EVIDENCE_RAW_DIR",
    "IOS_EVIDENCE_EXPECTED_VERSION",
    "IOS_DEVICE_UDID",
    "IOS_DEVICE_NAME",
    "IOS_SIGNED_IPA_PATH",
    "MOBILE_API_BASE_URL",
    "MOBILE_APPROVAL_CALLBACK_URL",
    "OMNI_IOS_BUNDLE_ID",
]

TRI_APP_LIVE_SMOKE_SECRETS = [
    "TRI_APP_ADMIN_TOKEN",
    "TRI_APP_MOBILE_APPROVAL_TOKEN",
    "TRI_APP_DESKTOP_AGENT_TOKEN",
    "TRI_APP_AUDIT_HMAC_SECRET",
]

TRI_APP_LIVE_SMOKE_VARS = [
    "TRI_APP_BACKEND_BASE_URL",
    "TRI_APP_WEB_ADMIN_BASE_URL",
    "TRI_APP_MOBILE_CALLBACK_URL",
    "TRI_APP_DESKTOP_AGENT_URL",
    "TRI_APP_ORG_ID",
    "TRI_APP_LIVE_SMOKE_SCENARIO_ID",
    "TRI_APP_LIVE_SMOKE_REPORT_PATH",
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
    | set(WEB_ADMIN_SECRETS)
    | set(WEB_ADMIN_VARS)
    | set(DESKTOP_SECRETS)
    | set(DESKTOP_VARS)
    | set(MOBILE_SECRETS)
    | set(MOBILE_VARS)
    | set(TRI_APP_SECRETS)
    | set(TRI_APP_VARS)
    | set(IOS_EVIDENCE_VARS)
    | set(MOBILE_REAL_DEVICE_SECRETS)
    | set(MOBILE_REAL_DEVICE_VARS)
    | set(TRI_APP_LIVE_SMOKE_SECRETS)
    | set(TRI_APP_LIVE_SMOKE_VARS)
    | {"EXPECTED_VERSION"}
)


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


def _parse_url(value: str):
    return urlparse(value)


def _is_http_url(value: str) -> bool:
    parsed = _parse_url(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_local_host(hostname: str | None) -> bool:
    return (hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _is_https_or_local_http_url(value: str) -> bool:
    parsed = _parse_url(value)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme == "http" and parsed.netloc and _is_local_host(parsed.hostname):
        return True
    return False


def _is_origin(value: str) -> bool:
    parsed = _parse_url(value)
    if not _is_https_or_local_http_url(value):
        return False
    return parsed.path in {"", "/"} and not parsed.params and not parsed.query and not parsed.fragment


def _validate_url_var(
    name: str,
    issues: list[Issue],
    *,
    https_or_local: bool = False,
    origin_only: bool = False,
) -> None:
    value = _value(name)
    if not value:
        return
    ok = _is_origin(value) if origin_only else (_is_https_or_local_http_url(value) if https_or_local else _is_http_url(value))
    if not ok:
        message = f"invalid var: {name} must be an http(s) URL with a host"
        if origin_only:
            message = f"invalid var: {name} must be an https origin or localhost http origin without path/query"
        elif https_or_local:
            message = f"invalid var: {name} must be https or localhost http with a host"
        _issue(issues, severity="blocker", kind="invalid_var", name=name, message=message)


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


def _validate_digest_pinned_image_var(name: str, issues: list[Issue], *, context: str) -> None:
    value = _value(name)
    if value and not _is_digest_pinned_image_ref(value):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name=name,
            message=f"invalid var: {context} {name} must be pinned by digest",
        )


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
        _validate_url_var(name, issues)


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
    if normalized != value or ".." in PurePosixPath(value).parts or "." in PurePosixPath(value).parts:
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
    if deploy_mode == "kubectl":
        _validate_digest_pinned_image_var("OMNIDESK_IMAGE", issues, context="kubectl")
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


def _validate_web_admin_shapes(issues: list[Issue]) -> None:
    _validate_url_var("WEB_ADMIN_BASE_URL", issues, https_or_local=True)
    _validate_url_var("WEB_ADMIN_API_BASE_URL", issues, https_or_local=True)
    _validate_digest_pinned_image_var("WEB_ADMIN_IMAGE", issues, context="web-admin")


def _validate_desktop_shapes(issues: list[Issue]) -> None:
    _validate_url_var("DESKTOP_AGENT_BASE_URL", issues, https_or_local=True)
    _validate_url_var("DESKTOP_UPDATE_ENDPOINT", issues, https_or_local=True)
    _validate_url_var("DESKTOP_BRIDGE_ORIGIN", issues, origin_only=True)
    app_id = _value("DESKTOP_APP_IDENTIFIER")
    if app_id and not REVERSE_DNS_RE.fullmatch(app_id):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="DESKTOP_APP_IDENTIFIER",
            message="invalid var: DESKTOP_APP_IDENTIFIER must be a reverse-DNS app identifier",
        )


def _validate_mobile_shapes(issues: list[Issue]) -> None:
    _validate_url_var("MOBILE_API_BASE_URL", issues, https_or_local=True)
    _validate_url_var("MOBILE_APPROVAL_CALLBACK_URL", issues, https_or_local=True)
    android_package = _value("OMNI_ANDROID_PACKAGE_NAME")
    if android_package and not ANDROID_PACKAGE_RE.fullmatch(android_package):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNI_ANDROID_PACKAGE_NAME",
            message="invalid var: OMNI_ANDROID_PACKAGE_NAME must be a lowercase Android package name",
        )
    ios_bundle = _value("OMNI_IOS_BUNDLE_ID")
    if ios_bundle and not REVERSE_DNS_RE.fullmatch(ios_bundle):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNI_IOS_BUNDLE_ID",
            message="invalid var: OMNI_IOS_BUNDLE_ID must be a reverse-DNS iOS bundle id",
        )


def _validate_tri_app_shapes(issues: list[Issue]) -> None:
    for name in [
        "TRI_APP_BACKEND_BASE_URL",
        "TRI_APP_WEB_ADMIN_BASE_URL",
        "TRI_APP_MOBILE_CALLBACK_URL",
        "TRI_APP_DESKTOP_AGENT_URL",
    ]:
        _validate_url_var(name, issues, https_or_local=True)
    org_id = _value("TRI_APP_ORG_ID")
    if org_id and not ORG_ID_RE.fullmatch(org_id):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="TRI_APP_ORG_ID",
            message="invalid var: TRI_APP_ORG_ID must be 3-64 safe characters",
        )


def _validate_ios_evidence_raw_dir(issues: list[Issue]) -> None:
    raw = _value("IOS_EVIDENCE_RAW_DIR")
    if not raw:
        return
    raw_path = Path(raw).expanduser()
    if not raw_path.exists() or not raw_path.is_dir():
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="IOS_EVIDENCE_RAW_DIR",
            message="invalid var: IOS_EVIDENCE_RAW_DIR must point to an existing directory",
        )
        return
    for rel in IOS_EVIDENCE_REQUIRED_RELATIVE_FILES:
        if not (raw_path / rel).exists():
            _issue(
                issues,
                severity="blocker",
                kind="missing_evidence",
                name="IOS_EVIDENCE_RAW_DIR",
                message=f"missing iOS real-device evidence file under IOS_EVIDENCE_RAW_DIR: {rel}",
            )
    # 1.11.8 closure: ios-evidence preflight must validate semantic evidence, not only file presence.
    try:
        from scripts.import_ios_real_device_evidence import validate_raw_dir

        report = validate_raw_dir(raw_path, expected_version=_value("IOS_EVIDENCE_EXPECTED_VERSION") or None)
        for rel, result in report.get("files", {}).items():
            for detail in result.get("issues", []):
                _issue(
                    issues,
                    severity="blocker",
                    kind="invalid_evidence",
                    name="IOS_EVIDENCE_RAW_DIR",
                    message=f"invalid iOS real-device evidence {rel}: {detail}",
                )
        for detail in report.get("consistency", {}).get("issues", []):
            _issue(
                issues,
                severity="blocker",
                kind="invalid_evidence",
                name="IOS_EVIDENCE_RAW_DIR",
                message=f"invalid iOS real-device evidence consistency: {detail}",
            )
    except Exception as exc:  # noqa: BLE001 - preflight must fail closed when semantic validation cannot run
        _issue(
            issues,
            severity="blocker",
            kind="invalid_evidence",
            name="IOS_EVIDENCE_RAW_DIR",
            message=f"invalid iOS real-device evidence validation failed: {exc}",
        )


def _validate_mobile_real_device_shapes(issues: list[Issue]) -> None:
    _validate_ios_evidence_raw_dir(issues)
    _validate_url_var("MOBILE_API_BASE_URL", issues, https_or_local=True)
    _validate_url_var("MOBILE_APPROVAL_CALLBACK_URL", issues, https_or_local=True)
    ios_bundle = _value("OMNI_IOS_BUNDLE_ID")
    if ios_bundle and not REVERSE_DNS_RE.fullmatch(ios_bundle):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="OMNI_IOS_BUNDLE_ID",
            message="invalid var: OMNI_IOS_BUNDLE_ID must be a reverse-DNS iOS bundle id",
        )
    ipa = _value("IOS_SIGNED_IPA_PATH")
    if ipa and not ipa.endswith(".ipa"):
        _issue(
            issues,
            severity="blocker",
            kind="invalid_var",
            name="IOS_SIGNED_IPA_PATH",
            message="invalid var: IOS_SIGNED_IPA_PATH must point to a signed .ipa artifact",
        )


def _validate_tri_app_live_smoke_shapes(issues: list[Issue]) -> None:
    _validate_tri_app_shapes(issues)
    report_path = _value("TRI_APP_LIVE_SMOKE_REPORT_PATH")
    if report_path:
        candidate = PurePosixPath(report_path)
        if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
            _issue(
                issues,
                severity="blocker",
                kind="invalid_var",
                name="TRI_APP_LIVE_SMOKE_REPORT_PATH",
                message="invalid var: TRI_APP_LIVE_SMOKE_REPORT_PATH must be a safe relative report path without . or .. segments",
            )
            return
        concrete = Path(report_path)
        if not concrete.exists():
            _issue(
                issues,
                severity="blocker",
                kind="missing_evidence",
                name="TRI_APP_LIVE_SMOKE_REPORT_PATH",
                message="missing tri-app live smoke report at TRI_APP_LIVE_SMOKE_REPORT_PATH",
            )
            return
        try:
            from scripts.import_tri_app_live_smoke_evidence import validate_report_file

            result = validate_report_file(
                concrete,
                expected_org_id=_value("TRI_APP_ORG_ID") or None,
                expected_scenario_id=_value("TRI_APP_LIVE_SMOKE_SCENARIO_ID") or None,
            )
            for detail in result.get("issues", []):
                _issue(
                    issues,
                    severity="blocker",
                    kind="invalid_evidence",
                    name="TRI_APP_LIVE_SMOKE_REPORT_PATH",
                    message=f"invalid tri-app live smoke evidence: {detail}",
                )
        except Exception as exc:  # noqa: BLE001
            _issue(
                issues,
                severity="blocker",
                kind="invalid_evidence",
                name="TRI_APP_LIVE_SMOKE_REPORT_PATH",
                message=f"invalid tri-app live smoke evidence validation failed: {exc}",
            )


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
    _require(WEB_ADMIN_VARS, "var", issues)
    _validate_web_admin_shapes(issues)
    return issues


def check_desktop() -> list[Issue]:
    issues: list[Issue] = []
    _require(DESKTOP_SECRETS, "secret", issues)
    _require(DESKTOP_VARS, "var", issues)
    _validate_desktop_shapes(issues)
    return issues


def check_mobile() -> list[Issue]:
    issues: list[Issue] = []
    _require(MOBILE_SECRETS, "secret", issues)
    _require(MOBILE_VARS, "var", issues)
    _validate_mobile_shapes(issues)
    return issues


def check_tri_app() -> list[Issue]:
    issues: list[Issue] = []
    _require(TRI_APP_SECRETS, "secret", issues)
    _require(TRI_APP_VARS, "var", issues)
    _validate_tri_app_shapes(issues)
    return issues


def check_ios_evidence() -> list[Issue]:
    issues: list[Issue] = []
    _require(IOS_EVIDENCE_VARS, "var", issues)
    _validate_ios_evidence_raw_dir(issues)
    return issues


def check_mobile_real_device() -> list[Issue]:
    issues: list[Issue] = []
    _require(MOBILE_REAL_DEVICE_SECRETS, "secret", issues)
    _require(MOBILE_REAL_DEVICE_VARS, "var", issues)
    _validate_mobile_real_device_shapes(issues)
    return issues


def check_tri_app_live_smoke() -> list[Issue]:
    issues: list[Issue] = []
    _require(TRI_APP_LIVE_SMOKE_SECRETS, "secret", issues)
    _require(TRI_APP_LIVE_SMOKE_VARS, "var", issues)
    _validate_tri_app_live_smoke_shapes(issues)
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
    path = os.fspath(report_path)
    target = __import__("pathlib").Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    parser = argparse.ArgumentParser(description="Fail fast when release, downstream, or tri-app GitHub configuration is incomplete.")
    parser.add_argument(
        "--scope",
        choices=["release", "staging", "production", "rollback", "web-admin", "desktop", "mobile", "tri-app", "ios-evidence", "mobile-real-device", "tri-app-live-smoke"],
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
    elif args.scope == "mobile":
        issues = check_mobile()
    elif args.scope == "tri-app":
        issues = check_tri_app()
    elif args.scope == "ios-evidence":
        issues = check_ios_evidence()
    elif args.scope == "mobile-real-device":
        issues = check_mobile_real_device()
    else:
        issues = check_tri_app_live_smoke()

    payload = _result_payload(args.scope, args.deploy_mode, issues)
    _write_report(args.report_path, payload)

    if args.format == "json":
        _emit_json(payload, stream=sys.stderr if issues else sys.stdout)
    else:
        _emit_text(args.scope, issues)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
