#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REAL_EVIDENCE_DIR = Path("release/external-evidence")
DEFAULT_MANIFEST = Path("release/external-ga-evidence.required.json")


def _load_required_files(root: Path, manifest_path: Path) -> list[str]:
    path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files: list[str] = []
    for value in (manifest.get("required_files") or {}).values():
        if isinstance(value, list):
            files.extend(str(item) for item in value)
    return sorted(dict.fromkeys(files))


def _base_template(rel_path: str) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "status": "replace-with-passed-from-real-run",
        "produced_at": "replace-with-iso-8601-time",
        "producer": "replace-with-ci-run-or-operator-id",
        "environment": "staging-or-production",
        "expected_evidence_file": f"release/external-evidence/{rel_path}",
        "_template_notice": "Template only. It is not Real GA evidence until every field is replaced by output from a real CI, signer, provider, staging cluster, integration, or operations drill.",
    }
    if rel_path.startswith("native-build/"):
        doc.update({
            "schema": "omnidesk-native-build-evidence/v1",
            "command": "replace-with-command",
            "exit_code": 0,
            "source_commit": "replace-with-source-commit",
            "build_run_id": "replace-with-build-run-id",
            "release_payload_artifact_sha256": "replace-with-release-payload-sha256",
            "artifacts": [{"path": "replace-with-artifact-path", "sha256": "replace-with-artifact-sha256"}],
        })
    elif rel_path.startswith("signed-artifacts/"):
        platform = "macos" if "macos" in rel_path else "windows" if "windows" in rel_path else "android" if "android" in rel_path else "ios"
        signature_fields = {
            "android": {"android_signer_certificate_sha256": "replace-with-android-signer-certificate-sha256"},
            "ios": {
                "apple_team_id": "replace-with-apple-team-id",
                "provisioning_profile_uuid": "replace-with-provisioning-profile-uuid",
                "ipa_codesign_identifier": "replace-with-ipa-codesign-identifier",
            },
            "macos": {
                "developer_id_application": "replace-with-developer-id-application",
                "notarization_submission_id": "replace-with-notarization-submission-id",
            },
            "windows": {
                "authenticode_signer": "replace-with-authenticode-signer",
                "authenticode_certificate_sha256": "replace-with-authenticode-certificate-sha256",
                "authenticode_verified": True,
            },
        }[platform]
        doc.update({
            "schema": "omnidesk-signed-artifact-evidence/v1",
            "platform": platform,
            "signature_verified": True,
            "notarization_verified": platform == "macos",
            "signer_identity": "replace-with-signer-identity",
            "source_commit": "replace-with-source-commit",
            "signing_run_id": "replace-with-signing-run-id",
            "signed_artifact_sha256": "replace-with-signed-artifact-sha256",
            "native_signed_binding_sha256": "replace-with-the-same-final-artifact-sha256",
            "artifact_attestation": {
                "attestation_id": "replace-with-attestation-id",
                "subject_sha256": "replace-with-the-same-final-artifact-sha256",
            },
            "artifacts": [{"path": "replace-with-signed-artifact-path", "sha256": "replace-with-artifact-sha256"}],
            **signature_fields,
        })
    elif rel_path.startswith("control-plane/"):
        doc.update({"schema": "omnidesk-live-branch-protection/v2", "repository": "owner/repo", "branch": "main", "failures": []})
    elif rel_path.startswith("model/"):
        doc.update({"schema": "omnidesk-model-live-smoke/v1", "backend_base_url": "replace-with-live-backend", "scenario_id": "replace-with-scenario", "model_request_id": "replace-with-model-request-id", "trace_id": "replace-with-trace-id", "audit_event_id": "replace-with-audit-event-id", "cost_ledger_entry_id": "replace-with-cost-ledger-entry-id", "response_non_empty": True, "audit_logged": True, "cost_ledger_recorded": True, "budget_enforced": True, "approval_required_on_budget_exceeded": True, "p95_latency_ms": 2500, "error_rate": 0})
    elif rel_path.startswith("integrations/"):
        doc.update({"schema": "omnidesk-bigseller-live-smoke/v1", "store_id": "replace-with-store-id", "trace_id": "replace-with-trace-id", "audit_event_id": "replace-with-audit-event-id", "auth_success": True, "order_list_success": True, "inventory_list_success": True, "webhook_signature_verified": True, "webhook_replay_guard_verified": True, "secret_leakage_checked": True, "no_secret_leakage": True, "p95_latency_ms": 2500, "error_rate": 0})
    elif rel_path.startswith("push/"):
        provider = "apns" if "apns" in rel_path else "fcm"
        doc.update({"schema": "omnidesk-push-delivery-evidence/v1", "provider": provider, "delivery_success": True, "delivery_receipt_id": "replace-with-provider-receipt-id", "trace_id": "replace-with-trace-id"})
    elif rel_path.endswith("postgres-multi-instance-soak.json"):
        doc.update({"schema": "omnidesk-postgres-soak/v1", "gateway_count": 3, "worker_count": 2, "duration_minutes": 60, "critical_failures": 0})
    elif rel_path.endswith("rollback-drill.json"):
        doc.update({"schema": "omnidesk-rollback-drill/v1", "failed_rollout": True, "rollback_action": "replace-with-rollback-action", "slo_recovered": True, "recovery_verified": True})
    elif rel_path.endswith("backup-restore-drill.json"):
        doc.update({"schema": "omnidesk-backup-restore-drill/v1", "backup_verified": True, "restore_verified": True, "rpo_seconds": 300, "rto_seconds": 900})
    elif rel_path.endswith("self-healing-failure-injection.json"):
        doc.update({"schema": "omnidesk-self-healing-failure-injection/v1", "failure_injections": ["replace-with-controlled-failure"], "containment_action": "replace-with-containment-action", "recovery_verified": True, "post_recovery_health": "passed"})
    return doc


def _safe_output_dir(root: Path, output_dir: Path) -> Path:
    out = output_dir if output_dir.is_absolute() else root / output_dir
    real_dir = (root / REAL_EVIDENCE_DIR).resolve()
    out_resolved = out.resolve()
    if out_resolved == real_dir or real_dir in out_resolved.parents:
        raise SystemExit("Refusing to write templates into release/external-evidence; that path is reserved for real evidence.")
    return out


def write_templates(root: Path, output_dir: Path, manifest_path: Path) -> dict[str, Any]:
    out = _safe_output_dir(root, output_dir)
    files = _load_required_files(root, manifest_path)
    for rel in files:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_base_template(rel), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# Real GA External Evidence Templates\n\n"
        "Generated templates are operator aids only. They are not accepted as customer-distribution GA evidence.\n\n"
        "After real evidence is collected, validate it with:\n\n"
        "```bash\npython scripts/check_external_ga_evidence.py .\n```\n",
        encoding="utf-8",
    )
    return {"status": "templates_written", "output_dir": str(out.relative_to(root) if out.is_relative_to(root) else out), "template_count": len(files), "files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write Real GA external evidence templates without fabricating evidence.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--output-dir", default="dist/external-evidence-templates")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--write-report", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = write_templates(root, Path(args.output_dir), Path(args.manifest))
    if args.write_report:
        target = Path(args.write_report)
        if not target.is_absolute():
            target = root / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
