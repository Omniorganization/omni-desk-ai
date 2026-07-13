#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALGORITHM_VERSION = "omnidesk-industrial-score/v2"
WEIGHTS = {
    "architecture": 0.12,
    "security": 0.16,
    "ci_and_tests": 0.16,
    "model_and_agent_runtime": 0.14,
    "tri_app_product": 0.12,
    "desktop_runtime": 0.10,
    "container_and_supply_chain": 0.10,
    "governance_and_release_contracts": 0.10,
}


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def text(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def exists(root: Path, *paths: str) -> float:
    if not paths:
        return 0.0
    return sum(1 for path in paths if (root / path).exists()) / len(paths)


def check_summary(checks: dict[str, Any], required: list[str]) -> dict[str, Any]:
    observed: dict[str, list[dict[str, Any]]] = {}
    for item in checks.get("check_runs", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name:
            observed.setdefault(name, []).append(item)

    def state_for(name: str) -> str:
        candidates = observed.get(name, [])
        completed = [item for item in candidates if item.get("status") == "completed"]
        selected = completed[-1] if completed else (candidates[-1] if candidates else None)
        if not selected:
            return "missing"
        if selected.get("status") != "completed":
            return "pending"
        return str(selected.get("conclusion") or "unknown")

    states = {name: state_for(name) for name in required}
    successes = sum(1 for state in states.values() if state == "success")
    return {
        "required_count": len(required),
        "success_count": successes,
        "success_ratio": successes / len(required) if required else 1.0,
        "states": states,
        "all_required_success": successes == len(required),
    }


def coverage_summary(coverage: dict[str, Any]) -> dict[str, Any]:
    totals = coverage.get("totals", {}) if isinstance(coverage, dict) else {}
    overall = float(totals.get("percent_covered", 0.0) or 0.0)
    files = coverage.get("files", {}) if isinstance(coverage, dict) else {}

    def file_pct(path: str) -> float:
        detail = files.get(path, {})
        summary = detail.get("summary", {}) if isinstance(detail, dict) else {}
        return float(summary.get("percent_covered", 0.0) or 0.0)

    critical = {
        "resource_guard": file_pct("omnidesk_agent/security/resource_guard.py"),
        "admin_auth": file_pct("omnidesk_agent/security/admin_auth.py"),
        "chat_service": file_pct("omnidesk_agent/appsync/chat_service.py"),
        "provider_streaming": file_pct("omnidesk_agent/models/provider_streaming.py"),
    }
    return {
        "overall_percent": round(overall, 2),
        "critical_files": {key: round(value, 2) for key, value in critical.items()},
        "global_gate_passed": overall >= 80.0,
        "critical_security_gate_passed": (
            critical["resource_guard"] >= 95.0 and critical["admin_auth"] >= 90.0
        ),
    }


def score_contracts(root: Path, checks: dict[str, Any], coverage: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    protection = load_json(root / ".github/branch-protection.required.json", {})
    required = list(protection.get("required_check_contexts", []))
    check_report = check_summary(checks, required)
    coverage_report = coverage_summary(coverage)

    chat_contract = text(root, "apps/shared/omni-app-api.contract.json")
    desktop_executor = text(root, "apps/desktop-tauri/src/executor.ts")
    desktop_worker = text(root, "apps/desktop-tauri/src/runtimeWorker.ts")
    web_docker = text(root, "apps/web-admin-next/Dockerfile")
    web_supply = text(root, ".github/workflows/web-admin-supply-chain.yml")

    dimensions: dict[str, int] = {}
    dimensions["architecture"] = round(
        100
        * (
            0.65
            + 0.35
            * exists(
                root,
                "omnidesk_agent/appsync/chat_service.py",
                "omnidesk_agent/models/router_streaming.py",
                "omnidesk_agent/security/chat_resource_guard.py",
                "apps/shared/omni-app-api.contract.json",
            )
        )
    )
    dimensions["security"] = round(
        min(
            100.0,
            82.0
            + 8.0 * float(coverage_report["critical_security_gate_passed"])
            + 5.0 * float("require_signed_commits" in json.dumps(protection))
            + 5.0 * float((root / "omnidesk_agent/security/device_request_signature.py").exists()),
        )
    )
    dimensions["ci_and_tests"] = round(
        min(
            100.0,
            70.0
            + 20.0 * float(check_report["success_ratio"])
            + 10.0 * min(1.0, float(coverage_report["overall_percent"]) / 80.0),
        )
    )
    dimensions["model_and_agent_runtime"] = round(
        78.0
        + 4.0 * float((root / "omnidesk_agent/appsync/chat_service.py").exists())
        + 4.0 * float((root / "omnidesk_agent/models/provider_streaming.py").exists())
        + 4.0 * float((root / "omnidesk_agent/models/router_streaming.py").exists())
        + 4.0 * float(not (root / "omnidesk_agent/appsync/streaming.py").exists())
        + 3.0 * float("/api/chat/stream" in chat_contract)
        + 3.0 * float("chat.reasoning.delta" in chat_contract)
    )
    dimensions["tri_app_product"] = round(
        76.0
        + 8.0 * float('"client_surfaces": ["desktop", "mobile", "web_admin"]' in chat_contract.replace("\n", " "))
        + 4.0 * float("streamChat" in text(root, "apps/desktop-tauri/src/api.ts"))
        + 4.0 * float("streamChat" in text(root, "apps/web-admin-next/lib/api.ts"))
        + 4.0 * float("streamChat" in text(root, "apps/mobile-flutter/lib/omni_api.dart"))
        + 4.0 * float("supports_cancellation" in chat_contract)
    )
    dimensions["desktop_runtime"] = round(
        72.0
        + 5.0 * float("file_operation" in desktop_executor)
        + 5.0 * float("renewTaskLease" in desktop_worker)
        + 5.0 * float("AbortController" in desktop_worker)
        + 5.0 * float("flushStatusOutbox" in desktop_worker)
        + 4.0 * float("patch_workspace_file" in text(root, "apps/desktop-tauri/src-tauri/src/main.rs"))
        + 4.0 * float("diff_workspace_file" in text(root, "apps/desktop-tauri/src-tauri/src/main.rs"))
    )
    dimensions["container_and_supply_chain"] = round(
        78.0
        + 5.0 * float("USER nextjs" in web_docker)
        + 5.0 * float("org.opencontainers.image.base.digest" in web_docker)
        + 4.0 * float("web-admin-sbom.spdx.json" in web_supply)
        + 4.0 * float("web-admin-slsa-provenance.json" in web_supply)
        + 4.0 * float("cosign attest" in web_supply)
    )
    dimensions["governance_and_release_contracts"] = round(
        80.0
        + 5.0 * float(protection.get("require_pull_request") is True)
        + 5.0 * float(protection.get("require_signed_commits") is True)
        + 5.0 * float(protection.get("require_codeowners_review") is True)
        + 5.0 * float((root / "release/production-evidence.manifest.json").exists())
    )
    dimensions = {key: max(0, min(100, value)) for key, value in dimensions.items()}
    evidence = {
        "required_checks": check_report,
        "coverage": coverage_report,
        "source_contract_files": {
            "chat_service": (root / "omnidesk_agent/appsync/chat_service.py").exists(),
            "provider_native_streaming": (root / "omnidesk_agent/models/provider_streaming.py").exists(),
            "tri_app_stream_contract": "/api/chat/stream" in chat_contract,
            "desktop_lifecycle": bool(desktop_worker),
            "web_supply_chain": bool(web_supply),
        },
    }
    return dimensions, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an evidence-bound OmniDesk industrial score.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--checks-json", required=True)
    parser.add_argument("--coverage-json", default="")
    parser.add_argument(
        "--external-evidence-manifest",
        default="release/production-evidence.manifest.json",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if len(args.source_commit) != 40 or not all(
        char in "0123456789abcdef" for char in args.source_commit.lower()
    ):
        raise SystemExit("source commit must be a full Git SHA")
    root = Path(args.root).resolve()
    checks = load_json(Path(args.checks_json), {})
    coverage = load_json(Path(args.coverage_json), {}) if args.coverage_json else {}
    manifest = load_json(root / args.external_evidence_manifest, {})
    dimensions, evidence = score_contracts(root, checks, coverage)
    source_score = round(
        sum(dimensions[name] * weight for name, weight in WEIGHTS.items()),
        2,
    )
    manifest_status = str(manifest.get("status") or "missing")
    real_ga_attested = manifest_status in {"passed", "real_ga_attested"}
    external_delivery_score = 100.0 if real_ga_attested else 45.0
    overall_score = round(source_score * 0.90 + external_delivery_score * 0.10, 2)
    status = (
        "customer_distribution_production_ga"
        if real_ga_attested and source_score >= 95
        else "source_complete_external_evidence_blocked"
        if source_score >= 95
        else "source_candidate_requires_remediation"
    )
    payload: dict[str, Any] = {
        "schema": ALGORITHM_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": args.repository,
        "source_commit": args.source_commit,
        "workflow_run_id": str(args.workflow_run_id),
        "status": status,
        "scores": {
            "dimensions": dimensions,
            "source_industrial_maturity": source_score,
            "external_delivery_evidence": external_delivery_score,
            "overall_with_external_evidence": overall_score,
        },
        "weights": WEIGHTS,
        "real_ga_external_evidence": {
            "manifest_status": manifest_status,
            "attested": real_ga_attested,
            "excluded_from_source_score": True,
        },
        "evidence": evidence,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": status,
                "source_score": source_score,
                "overall_score": overall_score,
                "output": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
