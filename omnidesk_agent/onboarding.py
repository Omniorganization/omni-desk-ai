from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from omnidesk_agent.config import AppConfig
from omnidesk_agent.channels.ecosystem import ecosystem_security_summary, resolve_channel

CheckStatus = Literal["pass", "warn", "blocked"]
EvidenceStatus = Literal["passed", "blocked_missing_external_evidence", "not_run"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str
    fix: str = ""
    category: str = "runtime"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    profile: str
    summary: dict[str, int]
    checks: list[DoctorCheck] = field(default_factory=list)
    safe_fixes_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile": self.profile,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
            "safe_fixes_applied": self.safe_fixes_applied,
        }


def _which(name: str, *, required: bool, category: str, fix: str = "") -> DoctorCheck:
    path = shutil.which(name)
    if path:
        return DoctorCheck(name=name, status="pass", detail=path, category=category)
    return DoctorCheck(
        name=name,
        status="blocked" if required else "warn",
        detail=f"{name} is not available on PATH",
        fix=fix or f"Install {name} and ensure it is available on PATH.",
        category=category,
    )


def _env(name: str, *, required: bool, category: str, fix: str) -> DoctorCheck:
    value = os.getenv(name)
    if value:
        return DoctorCheck(name=name, status="pass", detail="configured", category=category)
    return DoctorCheck(
        name=name,
        status="blocked" if required else "warn",
        detail=f"{name} is not set",
        fix=fix,
        category=category,
    )


def _file(path: Path, *, required: bool, category: str, fix: str) -> DoctorCheck:
    if path.exists():
        return DoctorCheck(name=str(path), status="pass", detail="present", category=category)
    return DoctorCheck(
        name=str(path),
        status="blocked" if required else "warn",
        detail="missing",
        fix=fix,
        category=category,
    )


def run_doctor(cfg: AppConfig, *, profile: str = "single-mac-ga-lab", fix: bool = False, root: Path | None = None) -> DoctorReport:
    """Run the source and host readiness doctor without fabricating external evidence."""

    root = (root or Path.cwd()).resolve()
    safe_fixes: list[str] = []
    if fix:
        cfg.ensure_dirs()
        safe_fixes.extend(
            [
                str(cfg.workspace.root),
                str(cfg.workspace.memory_db.parent),
                str(cfg.permissions.audit_log.parent),
            ]
        )

    checks: list[DoctorCheck] = [
        DoctorCheck("host_os", "pass", f"{platform.system()} {platform.machine()}", category="host"),
        _which("python3", required=True, category="runtime", fix="Install Python 3.11+ for the backend API and tests."),
        _which("node", required=True, category="web", fix="Install Node.js 20+ for Web Admin and Desktop frontend builds."),
        _which("npm", required=True, category="web", fix="Install npm with Node.js."),
        _which("rustc", required=profile != "source-only", category="desktop", fix="Install Rust stable for Tauri native builds."),
        _which("cargo", required=profile != "source-only", category="desktop", fix="Install Cargo with Rust stable."),
        _which("flutter", required=profile != "source-only", category="mobile", fix="Install Flutter stable for Android/iOS builds."),
        _which("xcodebuild", required=platform.system() == "Darwin" and profile != "source-only", category="ios", fix="Install Xcode and accept the license."),
        _which("pod", required=False, category="ios", fix="Install CocoaPods for Flutter iOS plugin integration."),
        _which("docker", required=False, category="ops", fix="Install Docker/Podman for soak, rollback, and backup drills."),
        _which("kubectl", required=False, category="ops", fix="Install kubectl for Kubernetes production drills."),
        _env(cfg.storage.postgres_dsn_env, required=profile == "enterprise", category="postgres", fix="Set a staging Postgres DSN and run the multi-instance soak drill."),
        _env("APNS_KEY_ID", required=False, category="push", fix="Configure APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID, and APNS_AUTH_KEY_PATH."),
        _env("FCM_SERVICE_ACCOUNT_JSON", required=False, category="push", fix="Configure FCM_SERVICE_ACCOUNT_JSON and attach live delivery receipts."),
        _env("ANDROID_KEYSTORE_PATH", required=False, category="signing", fix="Configure Android keystore env vars before release builds."),
        _env("IOS_SIGNING_IDENTITY", required=False, category="signing", fix="Configure Apple signing identity/provisioning profiles."),
        _env("APPLE_NOTARY_PROFILE", required=False, category="signing", fix="Configure notarytool keychain profile for macOS notarization."),
        _file(root / "release" / "external-ga-evidence.required.json", required=True, category="evidence", fix="Restore the external GA evidence contract."),
        _file(root / "AGENTS.md", required=True, category="codex", fix="Add repository-level AI coding guardrails."),
    ]
    counts = {"pass": 0, "warn": 0, "blocked": 0}
    for check in checks:
        counts[check.status] += 1
    return DoctorReport(ok=counts["blocked"] == 0, profile=profile, summary=counts, checks=checks, safe_fixes_applied=safe_fixes)


def build_onboarding_plan(cfg: AppConfig, *, profile: str = "single-mac-ga-lab") -> dict[str, Any]:
    report = run_doctor(cfg, profile=profile, fix=False)
    return {
        "profile": profile,
        "status": "ready_for_configuration" if report.summary["blocked"] == 0 else "blocked_by_host_or_external_evidence",
        "steps": [
            "Run omnidesk doctor --fix to create safe local directories.",
            "Run omnidesk evidence doctor and attach real native build, signing, push, soak, rollback, backup/restore, and self-healing evidence.",
            "Run omnidesk channel onboard <channel> for every enabled channel before accepting inbound tasks.",
            "Run omnidesk device pair <device-id> for desktop/mobile workers.",
            "Run locked backend, Web Admin, Desktop Tauri, and Flutter release build gates.",
        ],
        "doctor_summary": report.summary,
    }


def build_evidence_doctor(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    required = root / "release" / "external-ga-evidence.required.json"
    audit = root / "release" / "real-ga-evidence-audit-1.11.json"
    previous_audit = root / "release" / "real-ga-evidence-audit-1.10.json"
    status: EvidenceStatus = "not_run"
    audit_path = audit if audit.exists() else previous_audit
    if audit_path.exists() and "blocked_missing_external_evidence" in audit_path.read_text(encoding="utf-8"):
        status = "blocked_missing_external_evidence"
    elif audit_path.exists():
        status = "passed"
    return {
        "status": status,
        "required_contract_present": required.exists(),
        "audit_path": str(audit_path) if audit_path.exists() else "",
        "required_categories": [
            "native_build",
            "signed_artifacts",
            "push_delivery",
            "postgres_soak",
            "rollback_drill",
            "backup_restore_drill",
            "self_healing_failure_injection",
        ],
        "policy": "External GA evidence must be produced by CI, signing, push, staging, and drill systems; source packages must not mark missing evidence as passed.",
    }


def build_channel_onboarding_plan(channel: str) -> dict[str, Any]:
    entry = resolve_channel(channel)
    if entry is None:
        return {
            "channel": channel,
            "status": "blocked_unknown_channel",
            "required_controls": ["operator_pairing", "permission_approval_gate", "audit_log"],
            "steps": ["Define a channel capability matrix entry before enabling the channel."],
        }
    return {
        "channel": entry.name,
        "display_name": entry.display_name,
        "status": "pairing_required",
        "risk": entry.risk,
        "surfaces": entry.surfaces,
        "required_controls": entry.required_controls,
        "steps": [
            "Verify channel signature or OAuth binding.",
            "Create a pairing code for the sender or workspace.",
            "Record sender trust level and identity history.",
            "Enable only the actions allowed by the channel capability matrix.",
            "Require owner approval for high-risk actions and identity drift.",
        ],
        "ecosystem_summary": ecosystem_security_summary(include_reference=False),
    }


def build_device_pairing_challenge(device_id: str, *, channel: str = "local") -> dict[str, Any]:
    normalized = device_id.strip()
    if not normalized:
        return {"status": "blocked", "reason": "device_id is required"}
    return {
        "status": "challenge_required",
        "device_id": normalized,
        "channel": channel,
        "required_headers": ["X-OmniDesk-Device-Id", "X-OmniDesk-Device-Signature", "X-OmniDesk-Device-Nonce"],
        "steps": [
            "Generate or load the per-install Ed25519 keypair.",
            "Submit the public key through the enrollment endpoint.",
            "Complete pairing with a short-lived code.",
            "Use signed requests for sensitive desktop/mobile APIs.",
        ],
    }


def build_app_connection_plan(app: str) -> dict[str, Any]:
    return {
        "app": app,
        "status": "foreground_confirmation_required",
        "controls": ["operator_pairing", "foreground_confirmation", "ui_bridge_app_allowlist", "audit_log"],
        "steps": [
            "Confirm the app is in the UI Bridge allowlist.",
            "Bind the app surface to a paired sender/device.",
            "Require explicit foreground confirmation before UI automation.",
            "Record audit evidence for every high-risk action.",
        ],
    }
