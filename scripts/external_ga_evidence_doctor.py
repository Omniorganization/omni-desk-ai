#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_external_ga_evidence import REQUIRED_EVIDENCE, audit


FIELD_TEMPLATES: dict[str, dict[str, Any]] = {
    "native_build": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "command": "",
        "exit_code": None,
        "artifacts": [{"path": "", "sha256": ""}],
    },
    "signed_artifacts": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "platform": "",
        "signature_verified": False,
        "notarization_verified": False,
        "artifacts": [{"path": "", "sha256": ""}],
    },
    "web_admin_signed_oci_image": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "image_ref": "",
        "image_digest": "",
        "base_image_digest_pinned": False,
        "cosign_signature_verified": False,
        "sbom_attestation_verified": False,
        "slsa_attestation_verified": False,
        "non_root_runtime_verified": False,
        "healthcheck_verified": False,
        "read_only_runtime_verified": False,
    },
    "push_delivery": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "provider": "",
        "delivery_success": False,
        "delivery_receipt_id": "",
    },
    "postgres_soak": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "gateway_count": 0,
        "worker_count": 0,
        "duration_minutes": 0,
        "critical_failures": None,
    },
    "rollback_drill": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "failed_rollout": False,
        "rollback_action": "",
        "slo_recovered": False,
        "recovery_verified": False,
    },
    "backup_restore_drill": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "backup_verified": False,
        "restore_verified": False,
        "rpo_seconds": None,
        "rto_seconds": None,
    },
    "self_healing_failure_injection": {
        "status": "blocked_pending_real_run",
        "producer": "",
        "produced_at": "",
        "failure_injections": [],
        "containment_action": "",
        "recovery_verified": False,
        "post_recovery_health": "",
    },
}


RUNBOOK_HINTS = {
    "native_build": "Run the platform-native CI build/check command and attach command, exit_code, timestamp, producer, and artifact hashes.",
    "signed_artifacts": "Run real platform signing/notarization CI and attach signed artifact paths plus verified signatures.",
    "web_admin_signed_oci_image": "Verify the pushed Web Admin OCI digest, Cosign signature, SBOM attestation, SLSA attestation, runtime user, healthcheck, and read-only filesystem.",
    "push_delivery": "Send live APNS/FCM notifications in staging and record provider receipts.",
    "postgres_soak": "Run at least 3 gateways and 2 workers for 60 minutes against Postgres and record zero critical failures.",
    "rollback_drill": "Trigger a controlled failed rollout, execute rollback, and record SLO recovery.",
    "backup_restore_drill": "Restore from a real backup and record verified RPO/RTO.",
    "self_healing_failure_injection": "Inject a controlled runtime failure and record containment plus post-recovery health.",
}


def _template_for(category: str, rel_path: str) -> dict[str, Any]:
    template = dict(FIELD_TEMPLATES[category])
    template["evidence_file"] = rel_path
    template["category"] = category
    template["policy"] = "Fill only with evidence produced by real external systems. This blocked template is not GA evidence."
    return template


def build_plan(root: Path, evidence_dir: Path) -> dict[str, Any]:
    report = audit(root, evidence_dir)
    missing: list[dict[str, Any]] = []
    for category, spec in REQUIRED_EVIDENCE.items():
        category_report = report["categories"][category]
        for item in category_report["files"]:
            if item["ok"]:
                continue
            rel_path = item["path"]
            missing.append(
                {
                    "category": category,
                    "label": spec["label"],
                    "path": rel_path,
                    "issues": item["issues"],
                    "required_fields": sorted(_template_for(category, rel_path).keys()),
                    "runbook_hint": RUNBOOK_HINTS[category],
                }
            )
    return {
        "status": "passed" if not missing else "blocked_missing_external_evidence",
        "evidence_dir": str(evidence_dir),
        "missing_count": len(missing),
        "missing": missing,
        "policy": "This doctor plans evidence collection only. It never converts missing evidence into passing evidence.",
    }


def write_plan(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_templates(root: Path, output_dir: Path) -> list[str]:
    written: list[str] = []
    for category, spec in REQUIRED_EVIDENCE.items():
        for rel_path in spec["files"]:
            target = output_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(_template_for(category, rel_path), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append(str(target.relative_to(root) if target.is_relative_to(root) else target))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a real-GA evidence collection plan and blocked JSON templates.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--evidence-dir", default="release/external-evidence")
    parser.add_argument("--write-plan", default="dist/external-ga-evidence-plan.json")
    parser.add_argument("--write-templates", default="dist/external-ga-evidence-templates")
    parser.add_argument("--no-templates", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = root / evidence_dir

    plan = build_plan(root, evidence_dir)
    plan_path = Path(args.write_plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    write_plan(plan_path, plan)

    if not args.no_templates:
        template_dir = Path(args.write_templates)
        if not template_dir.is_absolute():
            template_dir = root / template_dir
        plan["template_files"] = write_templates(root, template_dir)
        write_plan(plan_path, plan)

    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
