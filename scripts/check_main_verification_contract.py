#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_WORKFLOW_SNIPPETS = (
    "workflow_dispatch:",
    "push:",
    "branches:",
    "- main",
    "check_main_verification_contract.py .",
    "main-verification-evidence-${{ github.sha }}",
    "main-verification-evidence.json",
    "main-verification-artifact.json",
    "evidence_digest",
    "omnidesk-main-verification/v1",
    "omnidesk-main-verification-artifact/v1",
)

REQUIRED_CONTRACT_KEYS = (
    "schema",
    "artifact_manifest_schema",
    "artifact_name_pattern",
    "evidence_file",
    "artifact_manifest_file",
    "evidence_digest_algorithm",
    "required_for_customer_distribution_ga",
)


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict:
    return json.loads(_read(path))


def _assert(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the post-merge main verification evidence contract.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    workflow = _read(root / ".github" / "workflows" / "main-verification.yml")
    evidence_manifest = _json(root / "release" / "production-evidence.manifest.json")
    failures: list[str] = []

    for snippet in REQUIRED_WORKFLOW_SNIPPETS:
        _assert(snippet in workflow, f"main-verification.yml must contain {snippet!r}", failures)

    _assert(re.search(r"^on:\s*$", workflow, re.MULTILINE) is not None, "main-verification.yml must declare workflow triggers", failures)
    _assert("cancel-in-progress: false" in workflow, "main verification must not cancel in-progress evidence runs", failures)
    _assert("GITHUB_SHA" in workflow and "GITHUB_RUN_ID" in workflow and "GITHUB_RUN_ATTEMPT" in workflow, "main evidence must bind commit, run id, and run attempt", failures)
    _assert("hashlib.sha256" in workflow, "main verification must compute a sha256 evidence digest", failures)
    _assert("required_gates" in workflow, "main evidence must list the gates that were executed", failures)

    main_contract = evidence_manifest.get("main_verification_evidence")
    _assert(isinstance(main_contract, dict), "production-evidence manifest must declare main_verification_evidence", failures)
    if isinstance(main_contract, dict):
        for key in REQUIRED_CONTRACT_KEYS:
            _assert(key in main_contract, f"main_verification_evidence must declare {key}", failures)
        _assert(main_contract.get("schema") == "omnidesk-main-verification/v1", "main verification schema must match workflow evidence schema", failures)
        _assert(main_contract.get("artifact_manifest_schema") == "omnidesk-main-verification-artifact/v1", "main verification artifact schema must match workflow artifact schema", failures)
        _assert(str(main_contract.get("artifact_name_pattern", "")).endswith("-${commit_sha}"), "artifact name must be commit-addressed", failures)
        _assert(main_contract.get("evidence_digest_algorithm") == "sha256", "main verification evidence digest must use sha256", failures)
        _assert(main_contract.get("required_for_customer_distribution_ga") is True, "main verification evidence must be required for customer-distribution GA", failures)

    if failures:
        print("main verification contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("main verification contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
