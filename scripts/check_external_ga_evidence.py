#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


OK_STATUSES = {"ok", "passed", "success", "succeeded", "verified"}
PLACEHOLDER_RE = re.compile(r"\b(todo|tbd|placeholder|example|mock|fake|sample)\b", re.IGNORECASE)

REQUIRED_EVIDENCE: dict[str, dict[str, Any]] = {
    "native_build": {
        "label": "true Flutter/Rust/Tauri native build",
        "files": [
            "native-build/flutter-android-release.json",
            "native-build/flutter-ios-release.json",
            "native-build/tauri-desktop-release.json",
            "native-build/rust-cargo-check-locked.json",
        ],
        "requires_artifact": True,
    },
    "signed_artifacts": {
        "label": "true Android/iOS/Desktop signed artifacts",
        "files": [
            "signed-artifacts/android-signed-aab.json",
            "signed-artifacts/ios-signed-ipa.json",
            "signed-artifacts/desktop-macos-notarized.json",
            "signed-artifacts/desktop-windows-signed.json",
        ],
        "requires_artifact": True,
    },
    "live_branch_protection": {
        "label": "true GitHub branch protection control-plane verification",
        "files": ["control-plane/github-branch-protection-live.json"],
    },
    "model_live_smoke": {
        "label": "true live model Q&A smoke with audit and budget ledger evidence",
        "files": ["model/model-live-smoke.json"],
    },
    "bigseller_live_smoke": {
        "label": "true BigSeller staging smoke with auth, data, webhook, trace, audit, and leakage proof",
        "files": ["integrations/bigseller-live-smoke.json"],
    },
    "push_delivery": {
        "label": "true APNS/FCM push delivery",
        "files": ["push/apns-live-delivery.json", "push/fcm-live-delivery.json"],
    },
    "postgres_soak": {
        "label": "true multi-instance Postgres soak",
        "files": ["drills/postgres-multi-instance-soak.json"],
    },
    "rollback_drill": {
        "label": "true rollback drill",
        "files": ["drills/rollback-drill.json"],
    },
    "backup_restore_drill": {
        "label": "true backup/restore drill",
        "files": ["drills/backup-restore-drill.json"],
    },
    "self_healing_failure_injection": {
        "label": "true self-healing failure injection report",
        "files": ["drills/self-healing-failure-injection.json"],
    },
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not declare a project version")
    return match.group(1)


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def _status_ok(value: Any) -> bool:
    return str(value or "").strip().lower() in OK_STATUSES


def _bool_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "1", "verified", "passed", "ok"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_artifact(root: Path, evidence_dir: Path, artifact_path: str) -> Path:
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        return candidate
    for base in (evidence_dir, root):
        resolved = base / candidate
        if resolved.exists():
            return resolved
    return evidence_dir / candidate


def _validate_artifacts(root: Path, evidence_dir: Path, doc: dict[str, Any], requires_artifact: bool) -> list[str]:
    issues: list[str] = []
    artifacts = doc.get("artifacts") or []
    if isinstance(doc.get("signed_artifact"), str):
        artifacts.append({"path": doc["signed_artifact"], "sha256": doc.get("signed_artifact_sha256")})
    if requires_artifact and not artifacts:
        issues.append("artifact reference is required")
        return issues
    if not isinstance(artifacts, list):
        return ["artifacts must be a list"]
    for item in artifacts:
        if not isinstance(item, dict):
            issues.append("artifact entry must be an object")
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            issues.append("artifact path is required")
            continue
        artifact_path = _resolve_artifact(root, evidence_dir, path_value)
        if not artifact_path.exists():
            issues.append(f"artifact missing: {path_value}")
            continue
        expected_sha = str(item.get("sha256") or "").strip().lower()
        if expected_sha and expected_sha != _sha256(artifact_path):
            issues.append(f"artifact sha256 mismatch: {path_value}")
    return issues


def _validate_common(root: Path, evidence_dir: Path, path: Path, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    if not path.exists():
        return None, [f"missing evidence file: {path.relative_to(evidence_dir)}"]
    try:
        doc = _read_json(path)
    except Exception as exc:
        return None, [f"invalid json: {exc}"]
    if not _status_ok(doc.get("status")):
        issues.append("status must be passed/succeeded/verified")
    if not str(doc.get("produced_at") or "").strip() and "branch-protection-live" not in str(path):
        issues.append("produced_at is required")
    if not str(doc.get("producer") or "").strip() and "branch-protection-live" not in str(path):
        issues.append("producer is required")
    if _contains_placeholder(doc):
        issues.append("placeholder/mock/example values are not accepted as real evidence")
    issues.extend(_validate_artifacts(root, evidence_dir, doc, bool(spec.get("requires_artifact"))))
    return doc, issues


def _require_fields(doc: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f"{field} is required" for field in fields if not str(doc.get(field) or "").strip()]


def _category_specific(category: str, doc: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if category == "native_build":
        if int(doc.get("exit_code", 1)) != 0:
            issues.append("native build/check exit_code must be 0")
        if not str(doc.get("command") or "").strip():
            issues.append("native build command is required")
    elif category == "signed_artifacts":
        if not _bool_true(doc.get("signature_verified")):
            issues.append("signature_verified must be true")
        if "macos" in str(doc.get("platform", "")).lower() and not _bool_true(doc.get("notarization_verified")):
            issues.append("macOS notarization must be verified")
    elif category == "live_branch_protection":
        if doc.get("schema") not in {"omnidesk-live-branch-protection/v1", "omnidesk-live-branch-protection/v2", "omnidesk-live-branch-protection/v3"}:
            issues.append("schema must be omnidesk-live-branch-protection/v1, v2 or v3")
        issues.extend(_require_fields(doc, ("repository", "branch")))
        if doc.get("failures") not in ([], None):
            issues.append("live branch protection report must have no failures")
    elif category == "model_live_smoke":
        if doc.get("schema") not in (None, "omnidesk-model-live-smoke/v1"):
            issues.append("schema must be omnidesk-model-live-smoke/v1 when present")
        issues.extend(_require_fields(doc, ("environment", "backend_base_url", "scenario_id", "model_request_id", "trace_id", "audit_event_id", "cost_ledger_entry_id")))
        if str(doc.get("environment", "")).strip().lower() not in {"staging", "production", "prod"}:
            issues.append("environment must be staging or production")
        for field in ("response_non_empty", "audit_logged", "cost_ledger_recorded", "budget_enforced", "approval_required_on_budget_exceeded"):
            if not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
        try:
            if float(doc.get("p95_latency_ms")) <= 0 or float(doc.get("p95_latency_ms")) > 15000:
                issues.append("p95_latency_ms must be > 0 and <= 15000")
        except Exception:
            issues.append("p95_latency_ms must be numeric")
        try:
            if float(doc.get("error_rate")) < 0 or float(doc.get("error_rate")) > 0.01:
                issues.append("error_rate must be between 0 and 0.01")
        except Exception:
            issues.append("error_rate must be numeric")
    elif category == "bigseller_live_smoke":
        if doc.get("schema") != "omnidesk-bigseller-live-smoke/v1":
            issues.append("schema must be omnidesk-bigseller-live-smoke/v1")
        issues.extend(_require_fields(doc, ("environment", "store_id", "trace_id", "audit_event_id")))
        if str(doc.get("environment", "")).strip().lower() not in {"staging", "production", "prod"}:
            issues.append("environment must be staging or production")
        for field in (
            "auth_success",
            "order_list_success",
            "inventory_list_success",
            "webhook_signature_verified",
            "webhook_replay_guard_verified",
            "secret_leakage_checked",
            "no_secret_leakage",
        ):
            if not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
        try:
            if float(doc.get("p95_latency_ms")) <= 0 or float(doc.get("p95_latency_ms")) > 10000:
                issues.append("p95_latency_ms must be > 0 and <= 10000")
        except Exception:
            issues.append("p95_latency_ms must be numeric")
        try:
            if float(doc.get("error_rate")) < 0 or float(doc.get("error_rate")) > 0.01:
                issues.append("error_rate must be between 0 and 0.01")
        except Exception:
            issues.append("error_rate must be numeric")
    elif category == "push_delivery":
        if not _bool_true(doc.get("delivery_success")):
            issues.append("delivery_success must be true")
        issues.extend(_require_fields(doc, ("provider", "delivery_receipt_id")))
    elif category == "postgres_soak":
        if int(doc.get("gateway_count", 0)) < 3:
            issues.append("gateway_count must be >= 3")
        if int(doc.get("worker_count", 0)) < 2:
            issues.append("worker_count must be >= 2")
        if int(doc.get("duration_minutes", 0)) < 60:
            issues.append("duration_minutes must be >= 60")
        if int(doc.get("critical_failures", 1)) != 0:
            issues.append("critical_failures must be 0")
    elif category == "rollback_drill":
        for field in ("failed_rollout", "rollback_action", "slo_recovered", "recovery_verified"):
            if field.endswith("action"):
                if not str(doc.get(field) or "").strip():
                    issues.append(f"{field} is required")
            elif not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
    elif category == "backup_restore_drill":
        for field in ("backup_verified", "restore_verified"):
            if not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
        if doc.get("rpo_seconds") is None:
            issues.append("rpo_seconds is required")
        if doc.get("rto_seconds") is None:
            issues.append("rto_seconds is required")
    elif category == "self_healing_failure_injection":
        injections = doc.get("failure_injections") or []
        if not isinstance(injections, list) or not injections:
            issues.append("failure_injections must contain at least one controlled injection")
        if not str(doc.get("containment_action") or "").strip():
            issues.append("containment_action is required")
        if not _bool_true(doc.get("recovery_verified")):
            issues.append("recovery_verified must be true")
        if not _status_ok(doc.get("post_recovery_health")):
            issues.append("post_recovery_health must be passed/verified")
    return issues


def audit(root: Path, evidence_dir: Path) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    blocker_count = 0
    for category, spec in REQUIRED_EVIDENCE.items():
        file_results: list[dict[str, Any]] = []
        category_issues: list[str] = []
        for rel in spec["files"]:
            path = evidence_dir / rel
            doc, issues = _validate_common(root, evidence_dir, path, spec)
            if doc is not None:
                issues.extend(_category_specific(category, doc))
            if issues:
                category_issues.extend(issues)
            file_results.append({"path": rel, "ok": not issues, "issues": issues})
        ok = not category_issues
        if not ok:
            blocker_count += 1
        categories[category] = {
            "label": spec["label"],
            "ok": ok,
            "files": file_results,
            "issues": category_issues,
        }
    status = "passed" if blocker_count == 0 else "blocked_missing_external_evidence"
    return {
        "version": _project_version(root),
        "status": status,
        "evidence_dir": str(evidence_dir.relative_to(root) if evidence_dir.is_relative_to(root) else evidence_dir),
        "blocker_count": blocker_count,
        "categories": categories,
        "policy": "Customer-distribution GA requires every category to be verified from real external systems; source scaffolds are not evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate external GA evidence for customer-distribution readiness.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-dir", default="release/external-evidence")
    parser.add_argument("--audit-only", action="store_true", help="Return 0 even when evidence is missing; useful for source-package audits.")
    parser.add_argument("--write-report", help="Write JSON audit report to this path.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = root / evidence_dir
    report = audit(root, evidence_dir)
    if args.write_report:
        out = Path(args.write_report)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
