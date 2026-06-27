#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception as exc:  # pragma: no cover - dependency is installed in CI
    raise SystemExit(f"PyYAML is required for this check: {exc}")

HIGH_RISK_CAPABILITIES = ("shell", "browser", "ui_bridge", "gmail", "channels")
REQUIRED_ALWAYS_ASK_TOOLS = {"computer", "shell", "channels", "files", "ui_bridge", "browser", "gmail"}
PRODUCTION_CONFIGS = (
    "examples/config.production.yaml",
    "deploy/docker/config.production.example.yaml",
)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.exists():
        raise RuntimeError(f"missing required file: {rel}")
    return path.read_text(encoding="utf-8")


def _load_yaml(root: Path, rel: str) -> dict[str, Any]:
    loaded = yaml.safe_load(_read(root, rel))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{rel} must contain a YAML mapping")
    return loaded


def _get(mapping: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = mapping
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def _check_production_config(root: Path, rel: str, failures: list[str]) -> None:
    cfg = _load_yaml(root, rel)
    prefix = f"{rel}: "

    _check(_get(cfg, "gateway.allow_local_admin_without_token") is False, failures, prefix + "gateway.allow_local_admin_without_token must be false")
    _check(_get(cfg, "gateway.require_webhook_signatures") is True, failures, prefix + "gateway.require_webhook_signatures must be true")
    _check(bool(_get(cfg, "gateway.admin_token_env")), failures, prefix + "gateway.admin_token_env must be explicit")
    _check(bool(_get(cfg, "gateway.shared_secret_env")), failures, prefix + "gateway.shared_secret_env must be explicit")

    _check(_get(cfg, "storage.backend") == "postgres", failures, prefix + "storage.backend must be postgres")
    _check(_get(cfg, "storage.require_multi_instance_safe") is True, failures, prefix + "storage.require_multi_instance_safe must be true")
    _check(bool(_get(cfg, "storage.postgres_dsn_env")), failures, prefix + "storage.postgres_dsn_env must be explicit")

    _check(_get(cfg, "app_sync.backend") == "postgres", failures, prefix + "app_sync.backend must be postgres")
    _check(_get(cfg, "app_sync.require_idempotency") is True, failures, prefix + "app_sync.require_idempotency must be true")
    _check(_get(cfg, "app_sync.allow_websocket_query_auth") is False, failures, prefix + "app_sync.allow_websocket_query_auth must be false")
    _check(_get(cfg, "app_sync.require_device_public_key_in_production") is True, failures, prefix + "app_sync.require_device_public_key_in_production must be true")
    _check(_get(cfg, "app_sync.require_device_signed_requests_in_production") is True, failures, prefix + "app_sync.require_device_signed_requests_in_production must be true")
    _check(_get(cfg, "app_sync.reject_predictable_device_ids_in_production") is True, failures, prefix + "app_sync.reject_predictable_device_ids_in_production must be true")
    _check(int(_get(cfg, "app_sync.device_signature_max_skew_seconds", 999999)) <= 300, failures, prefix + "device signature skew must be <= 300 seconds")

    _check(_get(cfg, "api_resource_guard.enabled") is True, failures, prefix + "api_resource_guard.enabled must be true")
    _check(_get(cfg, "api_resource_guard.backend") == "postgres", failures, prefix + "api_resource_guard.backend must be postgres")
    _check(_get(cfg, "api_resource_guard.trusted_proxy_ips", []) == [], failures, prefix + "trusted_proxy_ips must default to []")
    for field in ("max_body_bytes", "max_requests_per_ip", "max_requests_per_endpoint", "max_requests_per_actor", "agent_run_max_requests_per_actor", "max_inflight_agent_runs"):
        _check(int(_get(cfg, f"api_resource_guard.{field}", 0) or 0) > 0, failures, prefix + f"api_resource_guard.{field} must be positive")

    _check(_get(cfg, "permissions.approval_mode") == "remote_approval", failures, prefix + "permissions.approval_mode must be remote_approval")
    _check(_get(cfg, "permissions.default_mode") == "ask", failures, prefix + "permissions.default_mode must be ask")
    _check(_get(cfg, "permissions.no_tty_mode") == "deny", failures, prefix + "permissions.no_tty_mode must be deny")
    _check(_get(cfg, "permissions.shell_backend") == "remote_docker", failures, prefix + "permissions.shell_backend must be remote_docker")
    always_ask = set(_get(cfg, "permissions.always_ask_tools", []) or [])
    _check(REQUIRED_ALWAYS_ASK_TOOLS.issubset(always_ask), failures, prefix + f"permissions.always_ask_tools missing {sorted(REQUIRED_ALWAYS_ASK_TOOLS - always_ask)}")
    _check("critical" in set(_get(cfg, "permissions.require_dual_approval_for_risks", []) or []), failures, prefix + "critical risk must require dual approval")
    _check(bool(_get(cfg, "permissions.audit_checkpoint_hmac_key_env")), failures, prefix + "permissions.audit_checkpoint_hmac_key_env must be explicit")

    _check(_get(cfg, "sandbox.backend") == "remote_docker", failures, prefix + "sandbox.backend must be remote_docker")
    _check(bool(_get(cfg, "sandbox.runner_token_env")), failures, prefix + "sandbox.runner_token_env must be explicit")
    _check(bool(_get(cfg, "sandbox.runner_hmac_secret_env")), failures, prefix + "sandbox.runner_hmac_secret_env must be explicit")
    _check(_get(cfg, "sandbox.docker_network") == "none", failures, prefix + "sandbox.docker_network must be none")

    _check(_get(cfg, "plugins.trusted_only") is True, failures, prefix + "plugins.trusted_only must be true")
    _check(_get(cfg, "plugins.allow_in_process") is False, failures, prefix + "plugins.allow_in_process must be false")
    _check(_get(cfg, "plugins.production_forbid_subprocess") is True, failures, prefix + "plugins.production_forbid_subprocess must be true")

    for capability in HIGH_RISK_CAPABILITIES:
        _check(_get(cfg, f"capabilities.{capability}.enabled") is False, failures, prefix + f"capabilities.{capability}.enabled must default to false in production templates")
    _check(_get(cfg, "capabilities.plugins.enabled") is True, failures, prefix + "capabilities.plugins.enabled must remain explicit")

    _check(_get(cfg, "memory_privacy.redact_pii") is True, failures, prefix + "memory_privacy.redact_pii must be true")
    _check(_get(cfg, "memory_privacy.isolate_by_actor") is True, failures, prefix + "memory_privacy.isolate_by_actor must be true")
    _check(_get(cfg, "memory_privacy.encrypt_at_rest") is True, failures, prefix + "memory_privacy.encrypt_at_rest must be true")
    _check(bool(_get(cfg, "memory_privacy.encryption_key_env")), failures, prefix + "memory_privacy.encryption_key_env must be explicit")

    if isinstance(_get(cfg, "channels.gmail"), dict):
        _check(_get(cfg, "channels.gmail.enabled") is False, failures, prefix + "channels.gmail.enabled must default to false")
        _check(_get(cfg, "channels.gmail.allow_send") is False, failures, prefix + "channels.gmail.allow_send must default to false")
        _check(_get(cfg, "channels.gmail.allow_compose") is False, failures, prefix + "channels.gmail.allow_compose must default to false")
    if isinstance(_get(cfg, "channels.chrome"), dict):
        _check(_get(cfg, "channels.chrome.enabled") is False, failures, prefix + "channels.chrome.enabled must default to false")
        _check(_get(cfg, "channels.chrome.forbid_default_profile") is True, failures, prefix + "channels.chrome.forbid_default_profile must be true")


def _check_helm_template(root: Path, failures: list[str]) -> None:
    rel = "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml"
    text = _read(root, rel)
    required_needles = {
        "gateway local admin disabled": "allow_local_admin_without_token: false",
        "webhook signatures required": "require_webhook_signatures: true",
        "remote approval": "approval_mode: remote_approval",
        "no tty deny": "no_tty_mode: deny",
        "remote shell backend": "shell_backend: remote_docker",
        "always ask tools": "always_ask_tools:",
        "sandbox remote docker": "backend: remote_docker",
        "sandbox network none": "docker_network: none",
        "postgres app sync": "app_sync:\n      backend: postgres",
        "device signatures": "require_device_signed_requests_in_production: true",
        "postgres storage": "storage:\n      backend: postgres",
        "trusted plugins only": "trusted_only: true",
        "no in process plugins": "allow_in_process: false",
        "forbid subprocess plugins": "production_forbid_subprocess: true",
        "shell disabled by default": "shell:\n        enabled: false",
        "browser disabled by default": "browser:\n        enabled: false",
        "ui bridge disabled by default": "ui_bridge:\n        enabled: false",
        "gmail disabled by default": "gmail:\n        enabled: false",
        "channels disabled by default": "channels:\n        enabled: false",
        "pii redaction": "redact_pii: true",
        "memory isolation": "isolate_by_actor: true",
        "memory encryption": "encrypt_at_rest: true",
    }
    for label, needle in required_needles.items():
        _check(needle in text, failures, f"{rel}: missing {label}")
    for tool in REQUIRED_ALWAYS_ASK_TOOLS:
        _check(f"        - {tool}" in text or f"        - {tool}\n" in text, failures, f"{rel}: always_ask_tools must include {tool}")


def _check_workflow_contracts(root: Path, failures: list[str]) -> None:
    checks = {
        ".github/workflows/security.yml": ("check_security_attack_surface.py .",),
        ".github/workflows/release-policy.yml": ("check_security_attack_surface.py .",),
        ".github/workflows/main-verification.yml": ("check_security_attack_surface.py .", "security_attack_surface"),
        ".github/workflows/security-attack-surface.yml": ("name: Security Attack Surface Gate", "security-attack-surface:"),
    }
    for rel, needles in checks.items():
        text = _read(root, rel)
        for needle in needles:
            _check(needle in text, failures, f"{rel}: missing {needle}")

    branch_policy = json.loads(_read(root, ".github/branch-protection.required.json"))
    status_checks = set(branch_policy.get("required_status_checks") or [])
    jobs = set(branch_policy.get("required_jobs") or [])
    _check("Security Attack Surface Gate" in status_checks, failures, "branch protection must require Security Attack Surface Gate")
    _check("security-attack-surface" in jobs, failures, "branch protection must require security-attack-surface job")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate production attack-surface security contracts.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    failures: list[str] = []

    for rel in PRODUCTION_CONFIGS:
        _check_production_config(root, rel, failures)
    _check_helm_template(root, failures)
    _check_workflow_contracts(root, failures)

    report = {
        "schema": "omnidesk-security-attack-surface/v1",
        "status": "passed" if not failures else "failed",
        "scope": "source_and_config_contract_not_runtime_penetration_test",
        "checked_configs": list(PRODUCTION_CONFIGS) + ["deploy/kubernetes/helm/omnidesk/templates/configmap.yaml"],
        "failures": failures,
        "boundary": "This gate prevents unsafe source/config regression. It does not replace live branch protection, signed artifact, push, soak, rollback, backup/restore, or failure-injection evidence.",
    }
    if args.write_report:
        path = Path(args.write_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure_count": len(failures)}, ensure_ascii=False, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"BLOCKER {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
