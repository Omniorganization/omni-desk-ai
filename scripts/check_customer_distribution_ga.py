#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.check_main_verification_artifact_live import audit as audit_live_main_verification
    from scripts.check_real_ga_complete import audit as audit_complete_real_ga
except ModuleNotFoundError:  # Direct execution sets scripts/ as sys.path[0].
    from check_main_verification_artifact_live import audit as audit_live_main_verification
    from check_real_ga_complete import audit as audit_complete_real_ga


def audit(
    root: Path,
    evidence_dir: Path,
    *,
    repository: str,
    commit_sha: str,
    token: str,
    api_base: str = "https://api.github.com",
    current_release_binding_report: Path | None = None,
) -> dict[str, Any]:
    """Validate the one non-bypassable Customer-distribution Real GA boundary."""
    complete = audit_complete_real_ga(root, evidence_dir)
    live = audit_live_main_verification(repository, commit_sha, token, api_base)
    categories = dict(complete.get("categories") or {})
    live_failures = list(live.get("failures") or [])
    live_ok = live.get("status") == "passed" and not live_failures
    categories["main_verification_live_artifact"] = {
        "label": "successful enforced Main Verification artifact for the exact release commit",
        "ok": live_ok,
        "files": [
            {
                "path": "dist/evidence/main-verification-live-artifact.json",
                "ok": live_ok,
                "issues": live_failures,
            }
        ],
        "issues": live_failures,
        "report": live,
    }
    binding_failures: list[str] = []
    binding: dict[str, Any] = {}
    if current_release_binding_report is None or not current_release_binding_report.is_file():
        binding_failures.append("current Release artifact binding report is required")
    else:
        try:
            value = json.loads(current_release_binding_report.read_text(encoding="utf-8"))
            binding = value if isinstance(value, dict) else {}
        except Exception as exc:
            binding_failures.append(f"current Release artifact binding report is invalid JSON: {exc}")
        if binding.get("schema") != "omnidesk-current-release-artifact-binding/v1":
            binding_failures.append("current Release artifact binding schema must be omnidesk-current-release-artifact-binding/v1")
        if binding.get("status") != "passed" or binding.get("all_artifacts_bound") is not True:
            binding_failures.append("current Release artifact binding must pass with all_artifacts_bound=true")
        if binding.get("repository") != repository:
            binding_failures.append("current Release artifact binding repository must match")
        if binding.get("source_commit") != commit_sha:
            binding_failures.append("current Release artifact binding source_commit must match")
        expected_release_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
        if expected_release_run_id and str(binding.get("release_run_id") or "") != expected_release_run_id:
            binding_failures.append("current Release artifact binding release_run_id must match this workflow run")
        if not str(binding.get("main_verification_run_id") or "").strip():
            binding_failures.append("current Release artifact binding main_verification_run_id is required")
        platforms = binding.get("platforms")
        reported_platforms = {
            str(item.get("platform") or "")
            for item in platforms or []
            if isinstance(item, dict) and item.get("status") == "passed"
        }
        if not isinstance(platforms, list) or reported_platforms != {"android", "ios", "macos", "windows"}:
            binding_failures.append("current Release artifact binding must pass android, ios, macos and windows exactly")
        if binding.get("failures") not in ([], None):
            binding_failures.append("current Release artifact binding report must have no failures")
    binding_ok = not binding_failures
    categories["current_release_artifact_binding"] = {
        "label": "current Release-built native files bound per digest before Customer GA and final payload signing",
        "ok": binding_ok,
        "files": [
            {
                "path": str(current_release_binding_report or "dist/current-release-artifact-binding.json"),
                "ok": binding_ok,
                "issues": binding_failures,
            }
        ],
        "issues": binding_failures,
        "report": binding,
    }
    blocker_count = sum(1 for category in categories.values() if not bool(category.get("ok")))
    return {
        "schema": "omnidesk-customer-distribution-ga/v1",
        "status": "passed" if blocker_count == 0 else "blocked_missing_external_evidence",
        "version": complete.get("version"),
        "repository": repository,
        "commit": commit_sha,
        "evidence_dir": str(evidence_dir),
        "blocker_count": blocker_count,
        "categories": categories,
        "policy": (
            "Customer-distribution Real GA requires the complete semantic external-evidence audit and an "
            "unexpired successful Main Verification workflow_dispatch artifact for the exact release commit, and "
            "a per-file digest match between this Release run, external signing evidence, attestations, and Main Verification."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the final Customer-distribution Real GA boundary.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-dir", default="release/external-evidence")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--api-base", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--write-report", default="")
    parser.add_argument("--write-live-report", default="")
    parser.add_argument("--current-release-binding-report", default="dist/current-release-artifact-binding.json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = root / evidence_dir
    current_release_binding_report = Path(args.current_release_binding_report)
    if not current_release_binding_report.is_absolute():
        current_release_binding_report = root / current_release_binding_report
    report = audit(
        root,
        evidence_dir,
        repository=args.repository,
        commit_sha=args.commit,
        token=os.environ.get(args.token_env, ""),
        api_base=args.api_base,
        current_release_binding_report=current_release_binding_report,
    )
    if args.write_report:
        output = Path(args.write_report)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if args.write_live_report:
        output = Path(args.write_live_report)
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        live_report = report["categories"]["main_verification_live_artifact"]["report"]
        output.write_text(json.dumps(live_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "blocker_count": report["blocker_count"]}, sort_keys=True))
    if report["status"] != "passed":
        for category_name, category in report["categories"].items():
            if not category.get("ok"):
                print(f"BLOCKER {category_name}", file=sys.stderr)
    return 0 if args.audit_only or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
