#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


CATEGORY_GATES = {
    "source_engineering": [
        ["scripts/check_version_consistency.py", "."],
        ["scripts/check_release_hygiene.py", ".", "--allow-vcs"],
        ["scripts/check_enterprise_readiness.py", "."],
        ["scripts/check_kubernetes_contract.py", "."],
    ],
    "release_governance": [
        ["scripts/check_release_channel_policy.py", "."],
        ["scripts/check_ci_evidence_contract.py", "."],
        ["scripts/check_branch_protection_contract.py", "."],
        ["scripts/check_main_verification_contract.py", "."],
    ],
    "security_supply_chain": [
        ["scripts/check_security_workflow_policy.py", "."],
        ["scripts/check_supply_chain_standard.py", "."],
        ["scripts/check_production_install_policy.py", "."],
    ],
    "tri_app_engineering": [
        ["scripts/check_tri_app_release_readiness.py", ".", "--mode", "source"],
    ],
    "offline_sync_reconnect": [
        ["-m", "pytest", "-q", "tests/test_offline_sync_updater_hardening.py", "tests/test_reconnect_sync_worker.py", "tests/test_appsync_offline_outbox.py"],
    ],
}


def _run(root: Path, args: list[str]) -> tuple[bool, str]:
    cmd = [sys.executable, *args]
    completed = subprocess.run(cmd, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = completed.stdout.strip()
    return completed.returncode == 0, output[-4000:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run source-only maturity closure gates for OmniDesk.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    results: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    for category, gates in CATEGORY_GATES.items():
        gate_results = []
        for gate in gates:
            passed, output = _run(root, gate)
            gate_name = " ".join(gate)
            gate_results.append({"gate": gate_name, "passed": passed, "output_tail": output})
            if not passed:
                failures.append(f"{category}: {gate_name}")
        results[category] = {
            "score": 100 if all(item["passed"] for item in gate_results) else 0,
            "gates": gate_results,
        }

    report = {
        "schema": "omnidesk-source-maturity-closure/v1",
        "status": "passed" if not failures else "failed",
        "scope": "source_only_not_customer_distribution_ga",
        "scores": {category: value["score"] for category, value in results.items()},
        "results": results,
        "distribution_ga_boundary": "External signer, store, push, rollback, backup/restore, soak, and failure-injection evidence remains a separate real-world gate.",
    }
    if args.write_report:
        Path(args.write_report).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "scores": report["scores"]}, ensure_ascii=False, sort_keys=True))
    if failures:
        for failure in failures:
            print(f"BLOCKER {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
