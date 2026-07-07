#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from typing import Any


def _load_external_ga_module():
    module_path = Path(__file__).resolve().parent / "check_external_ga_evidence.py"
    spec = importlib.util.spec_from_file_location("check_external_ga_evidence", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load external GA checker: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


external_ga = _load_external_ga_module()
audit_external_ga = external_ga.audit

OK_STATUSES = {"ok", "passed", "success", "succeeded", "verified"}

TEAM_GOVERNANCE_FILE = "control-plane/github-team-governance-live.json"
NATIVE_BINDING_FILE = "control-plane/native-signed-artifact-binding.json"
MAIN_VERIFICATION_FILE = "control-plane/main-verification-evidence.json"
TEAM_GOVERNANCE_CONTRACT = ".github/team-governance.required.json"

NATIVE_BUILD_EVIDENCE_PATHS = tuple(external_ga.REQUIRED_EVIDENCE["native_build"]["files"])
SIGNED_ARTIFACT_EVIDENCE_PATHS = tuple(external_ga.REQUIRED_EVIDENCE["signed_artifacts"]["files"])


def _status_ok(value: Any) -> bool:
    return str(value or "").strip().lower() in OK_STATUSES


def _bool_true(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"true", "yes", "1", "verified", "passed", "ok"}


def _read_json(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.exists():
        return None, [f"missing evidence file: {path}"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except Exception as exc:
        return None, [f"invalid json: {exc}"]


def _require_fields(doc: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f"{field} is required" for field in fields if not str(doc.get(field) or "").strip()]


def _common(doc: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not _status_ok(doc.get("status")):
        issues.append("status must be passed/succeeded/verified")
    if not str(doc.get("produced_at") or "").strip():
        issues.append("produced_at is required")
    if not str(doc.get("producer") or "").strip():
        issues.append("producer is required")
    return issues


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _expected_repository(root: Path) -> str:
    env_value = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_value:
        return env_value
    remote = _git_output(root, "remote", "get-url", "origin")
    if remote.endswith(".git"):
        remote = remote[:-4]
    marker = "github.com/"
    if marker in remote:
        return remote.rsplit(marker, 1)[1].strip("/")
    if remote.startswith("git@github.com:"):
        return remote.rsplit(":", 1)[1].strip("/")
    return ""


def _expected_commit(root: Path) -> str:
    env_value = os.environ.get("GITHUB_SHA", "").strip()
    if env_value:
        return env_value
    return _git_output(root, "rev-parse", "HEAD")


def _load_team_contract(root: Path) -> dict[str, Any]:
    path = root / TEAM_GOVERNANCE_CONTRACT
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _team_slugs(value: Any) -> set[str]:
    slugs: set[str] = set()
    if not isinstance(value, list):
        return slugs
    for item in value:
        if isinstance(item, str):
            slugs.add(item)
        elif isinstance(item, dict):
            slug = str(item.get("slug") or item.get("name") or "").strip()
            if slug:
                slugs.add(slug)
    return slugs


def _validate_evidence_rows(
    evidence_dir: Path,
    rows: Any,
    expected_paths: tuple[str, ...],
    label: str,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(rows, list):
        return [f"{label} must be a list"]
    rows_by_path = {str(row.get("path") or ""): row for row in rows if isinstance(row, dict)}
    missing = sorted(set(expected_paths) - set(rows_by_path))
    if missing:
        issues.append(f"{label} missing paths: {missing}")
    for rel_path in expected_paths:
        row = rows_by_path.get(rel_path)
        if not row:
            continue
        evidence_path = evidence_dir / rel_path
        if not evidence_path.exists():
            issues.append(f"{label} evidence file missing: {rel_path}")
            continue
        expected_digest = str(row.get("sha256") or "").strip()
        actual_digest = _sha256(evidence_path)
        if expected_digest != actual_digest:
            issues.append(f"{label} sha256 mismatch for {rel_path}")
        if not _bool_true(row.get("present")):
            issues.append(f"{label} present flag must be true for {rel_path}")
    return issues


def _team_governance(root: Path, evidence_dir: Path) -> dict[str, Any]:
    rel = TEAM_GOVERNANCE_FILE
    doc, issues = _read_json(evidence_dir / rel)
    if doc is not None:
        contract = _load_team_contract(root)
        expected_repository = _expected_repository(root)
        expected_commit = _expected_commit(root)
        expected_organization = str(contract.get("required_organization") or "").strip()
        expected_teams = _team_slugs(contract.get("required_teams"))
        issues.extend(_common(doc))
        if doc.get("schema") != "omnidesk-team-governance-live/v1":
            issues.append("schema must be omnidesk-team-governance-live/v1")
        issues.extend(_require_fields(doc, ("repository", "owner", "owner_type", "organization", "codeowners_ref")))
        if expected_repository and doc.get("repository") != expected_repository:
            issues.append(f"repository must be {expected_repository}")
        if expected_organization and doc.get("organization") != expected_organization:
            issues.append(f"organization must be {expected_organization}")
        if expected_commit and doc.get("codeowners_ref") != expected_commit:
            issues.append("codeowners_ref must match the checked commit")
        if str(doc.get("owner_type") or "") != "Organization":
            issues.append("owner_type must be Organization for Real GA")
        for field in (
            "repository_is_organization_owned",
            "required_teams_resolved",
            "codeowners_team_owned",
            "branch_protection_requires_codeowners_review",
            "admins_enforced",
        ):
            if not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
        if _bool_true(doc.get("personal_owner_fallback_active")):
            issues.append("personal_owner_fallback_active must be false for Real GA")
        required_teams = doc.get("required_teams") or []
        if not isinstance(required_teams, list) or len(required_teams) < 4:
            issues.append("required_teams must list all production GitHub teams")
        reported_teams = _team_slugs(required_teams)
        if expected_teams and reported_teams != expected_teams:
            issues.append(f"required_teams must match source contract teams: {sorted(expected_teams)}")
        if doc.get("failures") not in ([], None):
            issues.append("team governance live report must have no failures")
    return {
        "label": "true GitHub organization/team CODEOWNERS control-plane verification",
        "ok": not issues,
        "files": [{"path": rel, "ok": not issues, "issues": issues}],
        "issues": issues,
    }


def _native_signed_binding(root: Path, evidence_dir: Path) -> dict[str, Any]:
    rel = NATIVE_BINDING_FILE
    doc, issues = _read_json(evidence_dir / rel)
    if doc is not None:
        expected_commit = _expected_commit(root)
        issues.extend(_common(doc))
        if doc.get("schema") != "omnidesk-native-signed-artifact-binding/v1":
            issues.append("schema must be omnidesk-native-signed-artifact-binding/v1")
        issues.extend(_require_fields(doc, (
            "main_verification_commit",
            "main_verification_artifact_name",
            "main_verification_evidence_digest",
            "real_ga_evidence_summary",
        )))
        if expected_commit:
            if doc.get("main_verification_commit") != expected_commit:
                issues.append("main_verification_commit must match the checked commit")
            expected_artifact = f"main-verification-evidence-{expected_commit}"
            if doc.get("main_verification_artifact_name") != expected_artifact:
                issues.append(f"main_verification_artifact_name must be {expected_artifact}")
        digest = str(doc.get("main_verification_evidence_digest") or "").strip()
        main_verification_path = evidence_dir / MAIN_VERIFICATION_FILE
        if main_verification_path.exists() and digest != _sha256(main_verification_path):
            issues.append("main_verification_evidence_digest must match control-plane main-verification-evidence.json")
        for field in ("native_builds_bound", "signed_artifacts_bound", "all_required_native_builds_present", "all_required_signed_artifacts_present"):
            if not _bool_true(doc.get(field)):
                issues.append(f"{field} must be true")
        native_paths = doc.get("native_build_evidence_paths") or []
        signed_paths = doc.get("signed_artifact_evidence_paths") or []
        if not isinstance(native_paths, list) or set(native_paths) != set(NATIVE_BUILD_EVIDENCE_PATHS):
            issues.append("native_build_evidence_paths must match the required native build evidence files")
        if not isinstance(signed_paths, list) or set(signed_paths) != set(SIGNED_ARTIFACT_EVIDENCE_PATHS):
            issues.append("signed_artifact_evidence_paths must match the required signed artifact evidence files")
        issues.extend(
            _validate_evidence_rows(
                evidence_dir,
                doc.get("native_build_evidence"),
                NATIVE_BUILD_EVIDENCE_PATHS,
                "native_build_evidence",
            )
        )
        issues.extend(
            _validate_evidence_rows(
                evidence_dir,
                doc.get("signed_artifact_evidence"),
                SIGNED_ARTIFACT_EVIDENCE_PATHS,
                "signed_artifact_evidence",
            )
        )
    return {
        "label": "Main Verification binding for native build and signed artifact evidence",
        "ok": not issues,
        "files": [{"path": rel, "ok": not issues, "issues": issues}],
        "issues": issues,
    }


def audit(root: Path, evidence_dir: Path) -> dict[str, Any]:
    report = audit_external_ga(root, evidence_dir)
    categories = dict(report.get("categories") or {})
    categories["team_governance_control_plane"] = _team_governance(root, evidence_dir)
    categories["native_signed_artifact_bindings"] = _native_signed_binding(root, evidence_dir)
    blocker_count = sum(1 for category in categories.values() if not category.get("ok"))
    report["categories"] = categories
    report["blocker_count"] = blocker_count
    report["status"] = "passed" if blocker_count == 0 else "blocked_missing_external_evidence"
    report["policy"] = "Customer-distribution Real GA requires external evidence plus live organization/team governance and native signed artifact binding evidence."
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate complete Real GA evidence set.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-dir", default="release/external-evidence")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
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
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
