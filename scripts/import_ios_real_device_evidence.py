#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

OK_STATUSES = {"ok", "passed", "success", "succeeded", "verified"}
PLACEHOLDER_RE = re.compile(r"\b(REPLACE_WITH_|todo|tbd|placeholder|example|mock|fake|sample)\b", re.IGNORECASE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION = "1.11.8+source-gated-enterprise-chat-candidate"
SCHEMA_VERSION = "ios-real-device-evidence/v1"
APNS_ARTIFACT_KINDS = {
    "apns_provider_receipt",
    "device_notification_log",
    "firebase_delivery_receipt",
}

REQUIRED_FILES = {
    "native-build/flutter-ios-release.json": {
        "required": [
            "status",
            "produced_at",
            "producer",
            "platform",
            "version",
            "command",
            "exit_code",
            "artifacts",
            "smoke_cases",
        ],
        "bools": [
            "smoke_cases.archive_created",
            "smoke_cases.ipa_exported",
            "smoke_cases.codesign_metadata_present",
        ],
        "require_artifacts": True,
    },
    "signed-artifacts/ios-signed-ipa.json": {
        "required": [
            "status",
            "produced_at",
            "producer",
            "platform",
            "version",
            "signature_verified",
            "artifacts",
            "smoke_cases",
        ],
        "bools": [
            "signature_verified",
            "smoke_cases.install_to_real_device",
            "smoke_cases.launch_success",
            "smoke_cases.gateway_connect",
            "smoke_cases.device_enrollment",
            "smoke_cases.mobile_chat",
            "smoke_cases.approval_decision",
            "smoke_cases.biometric_or_pin_confirm",
        ],
        "require_artifacts": True,
    },
    "push/apns-live-delivery.json": {
        "required": [
            "status",
            "produced_at",
            "producer",
            "provider",
            "platform",
            "version",
            "delivery_success",
            "delivery_receipt_id",
            "artifacts",
            "smoke_cases",
        ],
        "bools": [
            "delivery_success",
            "smoke_cases.permission_requested",
            "smoke_cases.token_registered_with_gateway",
            "smoke_cases.provider_accepted_message",
            "smoke_cases.device_received_notification",
        ],
        "require_artifacts": True,
    },
}

SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|authorization|bearer|private[_-]?key|p8|p12|provisioning|udid|apns|fcm|firebase)",
    re.IGNORECASE,
)
ALLOWED_HASHED_SENSITIVE_KEY_RE = re.compile(r"(sha256|hash|hmac|fingerprint)$", re.IGNORECASE)


def _is_status_signal(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PLACEHOLDER_RE.search(value))
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    return False


def _get(doc: dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_artifact(raw_dir: Path, rel: str) -> tuple[Path | None, str | None]:
    if not rel:
        return None, "artifact path is required"
    candidate = Path(rel)
    if candidate.is_absolute():
        return None, f"artifact path must be relative to raw evidence dir: {rel}"
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None, f"artifact path must be canonical without . or .. segments: {rel}"
    root = raw_dir.resolve()
    resolved = (root / candidate).resolve()
    if not _is_within(resolved, root):
        return None, f"artifact path escapes raw evidence dir: {rel}"
    return resolved, None


def _validate_artifacts(raw_dir: Path, rel_doc: str, doc: dict[str, Any], *, require_artifacts: bool = True) -> list[str]:
    issues: list[str] = []
    artifacts = doc.get("artifacts") or []
    if not isinstance(artifacts, list) or not artifacts:
        return ["artifacts must contain at least one artifact reference"] if require_artifacts else []
    for item in artifacts:
        if not isinstance(item, dict):
            issues.append("artifact entry must be an object")
            continue
        rel = str(item.get("path") or "").strip()
        expected = str(item.get("sha256") or "").strip().lower()
        kind = str(item.get("kind") or "").strip()
        if rel_doc == "push/apns-live-delivery.json":
            if kind not in APNS_ARTIFACT_KINDS:
                allowed = ", ".join(sorted(APNS_ARTIFACT_KINDS))
                issues.append(f"APNS artifact kind must be one of {allowed}: {rel}")
            if Path(rel).suffix.lower() == ".ipa":
                issues.append(f"APNS delivery evidence artifact must not be an IPA: {rel}")
        resolved, path_issue = _resolve_artifact(raw_dir, rel)
        if path_issue:
            issues.append(path_issue)
            continue
        if not SHA256_RE.fullmatch(expected):
            issues.append(f"artifact sha256 must be a lowercase 64-char hash: {rel}")
            continue
        assert resolved is not None
        if not resolved.exists():
            issues.append(f"artifact file is missing: {rel}")
            continue
        if not resolved.is_file():
            issues.append(f"artifact path must point to a file: {rel}")
            continue
        actual = _sha256(resolved)
        if actual != expected:
            issues.append(f"artifact sha256 mismatch for {rel}: expected {expected}, got {actual}")
    return issues


def _validate_privacy(value: Any, path: str = "") -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                SENSITIVE_KEY_RE.search(str(key))
                and not ALLOWED_HASHED_SENSITIVE_KEY_RE.search(str(key))
                and not _is_status_signal(child)
            ):
                issues.append(f"sensitive raw field is not allowed in evidence: {child_path}")
            issues.extend(_validate_privacy(child, child_path))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            issues.extend(_validate_privacy(child, f"{path}[{i}]"))
    return issues


def validate_doc(raw_dir: Path, rel: str, doc: dict[str, Any], *, expected_version: str = VERSION) -> list[str]:
    spec = REQUIRED_FILES[rel]
    issues: list[str] = []
    if not isinstance(doc, dict):
        return ["evidence document must be a JSON object"]
    if str(doc.get("status") or "").strip().lower() not in OK_STATUSES:
        issues.append("status must be ok/passed/success/succeeded/verified")
    for field in spec["required"]:
        if _get(doc, field) in (None, "", []):
            issues.append(f"missing required field: {field}")
    for field in spec["bools"]:
        if _get(doc, field) is not True:
            issues.append(f"{field} must be true")
    if doc.get("platform") != "ios":
        issues.append("platform must be ios")
    if expected_version and doc.get("version") != expected_version:
        issues.append(f"version must be {expected_version}")
    if rel == "native-build/flutter-ios-release.json" and doc.get("exit_code") != 0:
        issues.append("exit_code must be 0")
    if _contains_placeholder(doc):
        issues.append("placeholder/mock/example values are not accepted as real evidence")
    schema = doc.get("schema_version")
    if schema not in (None, SCHEMA_VERSION):
        issues.append(f"schema_version must be {SCHEMA_VERSION} when present")
    issues.extend(_validate_privacy(doc))
    issues.extend(_validate_artifacts(raw_dir, rel, doc, require_artifacts=bool(spec.get("require_artifacts", True))))
    return issues


def _artifact_hashes(doc: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    artifacts = doc.get("artifacts") or []
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict) and isinstance(item.get("sha256"), str):
                hashes.add(item["sha256"].strip().lower())
    return hashes


def validate_raw_dir(raw_dir: Path, *, expected_version: str = VERSION) -> dict[str, Any]:
    raw_dir = raw_dir.resolve()
    report: dict[str, Any] = {"status": "passed", "files": {}, "consistency": {"ok": True, "issues": []}}
    docs: dict[str, dict[str, Any]] = {}
    for rel in REQUIRED_FILES:
        src = raw_dir / rel
        file_result: dict[str, Any] = {"path": rel, "source": str(src), "ok": False, "issues": []}
        if not src.exists():
            file_result["issues"].append(f"missing raw evidence file: {rel}")
        else:
            try:
                doc = _read_json(src)
                docs[rel] = doc
                issues = validate_doc(raw_dir, rel, doc, expected_version=expected_version)
                file_result["issues"] = issues
                file_result["ok"] = not issues
            except Exception as exc:  # noqa: BLE001 - CLI validation report should be explicit
                file_result["issues"].append(f"invalid evidence json: {exc}")
        if file_result["issues"]:
            report["status"] = "failed"
        report["files"][rel] = file_result

    consistency_issues: list[str] = []
    for field in ["version", "platform", "bundle_id", "git_commit"]:
        seen = {str(doc.get(field)) for doc in docs.values() if doc.get(field) not in (None, "")}
        if len(seen) > 1:
            consistency_issues.append(f"cross-evidence field mismatch for {field}: {sorted(seen)}")
    native = docs.get("native-build/flutter-ios-release.json")
    signed = docs.get("signed-artifacts/ios-signed-ipa.json")
    if native and signed:
        native_hashes = _artifact_hashes(native)
        signed_hashes = _artifact_hashes(signed)
        source_hash = str(signed.get("source_native_artifact_sha256") or "").strip().lower()
        if native_hashes and signed_hashes and not (native_hashes & signed_hashes) and source_hash not in native_hashes:
            consistency_issues.append(
                "signed IPA artifact must match native build artifact sha256 or declare source_native_artifact_sha256"
            )
    if consistency_issues:
        report["status"] = "failed"
        report["consistency"] = {"ok": False, "issues": consistency_issues}
    return report


def _copy_validated_files(raw_dir: Path, dest_dir: Path, report: dict[str, Any]) -> None:
    for rel, result in report.get("files", {}).items():
        if not result.get("ok"):
            continue
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raw_dir / rel, target)
        result["copied_to"] = str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import validated iOS real-device evidence into release/external-evidence.")
    parser.add_argument("--raw-dir", required=True, help="Directory containing the three raw iOS evidence JSON files and referenced artifacts.")
    parser.add_argument("--dest-dir", default="release/external-evidence")
    parser.add_argument("--copy", action="store_true", help="Copy validated evidence into dest-dir. Without this flag, only validate.")
    parser.add_argument("--expected-version", default=VERSION)
    parser.add_argument("--write-report", default="release/ios-real-device-evidence-import-report.json")
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir).resolve()
    dest_dir = Path(args.dest_dir).resolve()
    report_path = Path(args.write_report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path

    report = validate_raw_dir(raw_dir, expected_version=args.expected_version)
    if report["status"] == "passed" and args.copy:
        _copy_validated_files(raw_dir, dest_dir, report)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
