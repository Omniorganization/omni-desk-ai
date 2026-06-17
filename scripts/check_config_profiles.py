#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - import failure is environment-specific
    yaml = None


PROFILE_FILES = {
    "local": "examples/config.local.yaml",
    "staging": "examples/config.staging.yaml",
    "production": "examples/config.production.yaml",
    "enterprise": "examples/config.enterprise.yaml",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to validate config profiles")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _get(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _require(data: dict[str, Any], dotted: str, expected: Any, issues: list[str]) -> None:
    actual = _get(data, dotted)
    if actual != expected:
        issues.append(f"{dotted} must be {expected!r}, got {actual!r}")


def validate_profile(name: str, data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    _require(data, "permissions.default_mode", "ask", issues)
    _require(data, "permissions.no_tty_mode", "deny", issues)
    _require(data, "plugins.trusted_only", True, issues)
    if name == "local":
        _require(data, "gateway.host", "127.0.0.1", issues)
        return issues

    _require(data, "permissions.approval_mode", "remote_approval", issues)
    _require(data, "gateway.require_webhook_signatures", True, issues)
    _require(data, "gateway.allow_local_admin_without_token", False, issues)
    _require(data, "storage.backend", "postgres", issues)
    _require(data, "storage.require_multi_instance_safe", True, issues)
    _require(data, "app_sync.backend", "postgres", issues)
    _require(data, "app_sync.require_idempotency", True, issues)
    _require(data, "sandbox.backend", "remote_docker", issues)
    _require(data, "plugins.allow_in_process", False, issues)
    _require(data, "plugins.production_forbid_subprocess", True, issues)
    _require(data, "memory_privacy.redact_pii", True, issues)
    _require(data, "memory_privacy.isolate_by_actor", True, issues)
    if name in {"production", "enterprise"}:
        _require(data, "memory_privacy.encrypt_at_rest", True, issues)
        _require(data, "capabilities.browser.enabled", False, issues)
        _require(data, "capabilities.ui_bridge.enabled", False, issues)
        _require(data, "channels.chrome.enabled", False, issues)
    if name == "enterprise":
        dual = _get(data, "permissions.require_dual_approval_for_risks") or []
        if "critical" not in dual:
            issues.append("permissions.require_dual_approval_for_risks must include 'critical'")
        _require(data, "permissions.break_glass_enabled", True, issues)
    return issues


def validate_root(root: Path) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    total_issues = 0
    for name, rel_path in PROFILE_FILES.items():
        path = root / rel_path
        if not path.exists():
            profiles[name] = {"ok": False, "path": rel_path, "issues": [f"missing profile file: {rel_path}"]}
            total_issues += 1
            continue
        try:
            issues = validate_profile(name, _load_yaml(path))
        except Exception as exc:
            issues = [f"invalid profile yaml: {exc}"]
        profiles[name] = {"ok": not issues, "path": rel_path, "issues": issues}
        if issues:
            total_issues += 1
    return {
        "ok": total_issues == 0,
        "profile_issue_count": total_issues,
        "profiles": profiles,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OmniDesk configuration profile safety defaults.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    report = validate_root(Path(args.root).resolve())
    if report["ok"]:
        print("config profile validation ok")
        return 0
    print("config profile validation failed:", file=sys.stderr)
    for name, profile in report["profiles"].items():
        for issue in profile["issues"]:
            print(f"- {name}: {issue}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
