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
            "unexpired successful Main Verification workflow_dispatch artifact for the exact release commit."
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
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = root / evidence_dir
    report = audit(
        root,
        evidence_dir,
        repository=args.repository,
        commit_sha=args.commit,
        token=os.environ.get(args.token_env, ""),
        api_base=args.api_base,
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
