#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


# CodeQL must be pinned to a full commit SHA, with init and analyze on the
# same commit. The exact SHA is not hardcoded so Dependabot can bump it.
CODEQL_PIN_RE = re.compile(r"github/codeql-action/(init|analyze)@([0-9a-f]{40})\b")

SECURITY_SNIPPETS = [
    "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294",
    "github.com/zricethezav/gitleaks/v8@v8.28.0",
    "fetch-depth: 0",
    'gitleaks" detect --source . --no-git --redact --verbose --config .gitleaks.toml',
    'gitleaks" detect --source . --redact --verbose --config .gitleaks.toml',
    "scripts/check_security_exceptions.py release/security-exceptions",
    "scripts/check_license_policy.py --lockfile requirements.dev.lock --policy release/license-policy.json",
    "scripts/check_security_workflow_policy.py .",
    "scripts/check_security_attack_surface.py .",
    "security-events: write",
    "pull-requests: read",
    "scripts/check_glib_backport.py .",
    "pip-audit --disable-pip",
    "cargo audit",
    "github.com/google/osv-scanner/v2/cmd/osv-scanner@v2.4.0",
    "Enforce Python dependency audit",
    "Enforce ecosystem dependency audits",
    "security-core-${{ github.sha }}",
    "ecosystem-security-${{ github.sha }}",
]

ATTACK_SURFACE_WORKFLOW_SNIPPETS = [
    "name: Security Attack Surface Gate",
    "security-attack-surface:",
    "python scripts/check_security_attack_surface.py . --write-report dist/evidence/security-attack-surface.json",
    "tests/test_admin_auth.py",
    "tests/test_webhook_forced_signatures.py",
    "tests/test_device_signed_requests.py",
    "tests/test_agent_run_abuse_limits.py",
    "tests/test_files_path_escape_strict.py",
    "tests/test_browser_security.py",
]

LOCKFILES = [
    "requirements.lock",
    "requirements.runtime.lock",
    "requirements.bootstrap.lock",
    "requirements.dev.lock",
    "requirements.security.lock",
    "requirements.enterprise.lock",
]

DOCKER_SCAN_SNIPPETS = [
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
    "severity: HIGH,CRITICAL",
    'exit-code: "1"',
]

REQUIRED_FILES = [
    ".github/workflows/security.yml",
    ".github/workflows/security-attack-surface.yml",
    ".github/workflows/docker-scan.yml",
    ".gitleaks.toml",
    "release/license-policy.json",
    "scripts/check_glib_backport.py",
    "scripts/check_license_policy.py",
    "scripts/check_security_attack_surface.py",
    "scripts/check_security_exceptions.py",
]


# Production signing material (GitHub secret names). Only jobs that run from main
# inside the protected `release` environment may read it. Names on this path avoid
# "secret"/"key": CodeQL py/clear-text-logging-sensitive-data treats such names as
# sensitive and the issues below are printed.
SIGNING_MATERIAL_NAMES = (
    "OMNIDESK_RELEASE_SIGNING_ED25519_KEY",
    "OMNI_ANDROID_KEYSTORE_BASE64",
    "OMNI_ANDROID_KEYSTORE_PASSWORD",
    "OMNI_ANDROID_KEY_ALIAS",
    "OMNI_ANDROID_KEY_PASSWORD",
    "OMNI_IOS_DISTRIBUTION_CERT_BASE64",
    "OMNI_IOS_DISTRIBUTION_CERT_PASSWORD",
    "OMNI_IOS_PROVISIONING_PROFILE_BASE64",
    "OMNI_IOS_EXPORT_OPTIONS_PLIST_BASE64",
    "OMNI_IOS_KEYCHAIN_PASSWORD",
)
# Legacy HMAC signing lets every verifier forge releases; unsigned or HMAC
# release switches are for local development only.
FORBIDDEN_WORKFLOW_SNIPPETS = (
    "secrets.OMNIDESK_RELEASE_SIGNING_KEY",
    "OMNIDESK_RELEASE_SIGNATURE_ALGORITHM",
    "OMNIDESK_ALLOW_UNSIGNED_RELEASE",
)
MAIN_ONLY_IF = "github.ref == 'refs/heads/main'"
RELEASE_VERIFIER_WORKFLOWS = ("deploy-staging.yml", "promote-production.yml", "rollback-drill.yml")
# Workflows that download evidence from another run id, and their key in
# release/evidence-source-workflows.json.
EVIDENCE_CONSUMER_WORKFLOWS = {
    "release.yml": "release",
    "main-verification.yml": "main-verification",
    "remote-evidence-pipeline.yml": "remote-evidence-pipeline",
    "real-ga-evidence-control-plane.yml": "real-ga-evidence-control-plane",
    "real-ga-readiness.yml": "real-ga-readiness",
}
# Consumers that may take an artifact from their own in-progress run (allow_same_run).
# real-ga-readiness is read-only and is called by the control plane with github.run_id.
SAME_RUN_EVIDENCE_CONSUMERS = ("real-ga-readiness",)
EVIDENCE_SOURCE_POLICY = "release/evidence-source-workflows.json"
SIGNER_IDENTITY_RE = re.compile(r"RELEASE_SIGNER_IDENTITY_REGEXP:\s*'([^']*)'")
# The only jobs on main that may mint GitHub artifact attestations or keyless
# Sigstore signatures, each with the producer workflows whose runs it may take
# artifacts from by run id. A verifier that pins only the repository or owner
# accepts any of them, so each runs only from main in the release environment,
# is never triggered by a pull request or another run, and checks the source run
# before downloading from it.
ATTESTATION_SIGNER_JOBS: dict[str, dict[str, tuple[str, ...]]] = {
    "release.yml": {"build": ()},
    "web-first-release.yml": {"release": (".github/workflows/web-first-candidate.yml",)},
}
ATTESTATION_SIGNER_EVENTS = ("workflow_dispatch", "push")
SIGNING_ACTION_RE = re.compile(
    r"^(?:actions/attest(?:-build-provenance|-sbom)?@|sigstore/gh-action-sigstore-python@|slsa-framework/slsa-github-generator/)",
    re.IGNORECASE,
)
SIGNING_COMMAND_RE = re.compile(
    r"\b(?:cosign|sigstore)\b[\"')]*(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+(?:sign|sign-blob|attest|attest-blob)\b"
)
# Before each `gh run download` in a signer workflow, the same step must look the
# run up and pin all of these fields plus a producer registered for the job.
# Other downloads by run id go through scripts/fetch_ci_evidence_artifact.py.
SOURCE_RUN_CHECK_SNIPPETS = (
    "actions/runs/",
    "'head_branch': 'main'",
    "'head_sha'",
    "'conclusion': 'success'",
    "'event'",
)


def _pytest_collection_issues(root: Path) -> list[str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").exists() else ""
    section = text.split("[tool.pytest.ini_options]", 1)[1] if "[tool.pytest.ini_options]" in text else ""
    section = section.split("\n[", 1)[0]

    def array(key: str) -> list[str] | None:
        match = re.search(rf"^{key}\s*=\s*(\[[^\]]*\])", section, re.MULTILINE)
        try:
            return json.loads(match.group(1)) if match else None
        except ValueError:
            return None

    issues: list[str] = []
    if array("testpaths") != ["tests"]:
        issues.append('pyproject.toml: pytest must collect only testpaths = ["tests"] (release jobs download artifacts into the workspace)')
    norecurse = array("norecursedirs") or []
    for directory in ("release", "dist"):
        if directory not in norecurse:
            issues.append(f"pyproject.toml: pytest norecursedirs must include {directory}")
    return issues


def _load_workflow(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _job_environment(job: dict[str, Any]) -> str:
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return str(environment or "")


def _reads_signing_material(text: str) -> bool:
    return any(f"secrets.{material}" in text for material in SIGNING_MATERIAL_NAMES)


def _signing_job_scope_issues(name: str, workflow: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if _reads_signing_material(yaml.safe_dump(workflow.get("env") or {})):
        issues.append(f"{name}: release signing secrets must not be exposed through workflow-level env")
    for job_id, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        # Only report that a job reads signing material, never which names.
        if not _reads_signing_material(yaml.safe_dump(job, sort_keys=False)):
            continue
        if _job_environment(job) != "release":
            issues.append(f"{name}: job {job_id} reads release signing secrets outside the protected release environment")
        if str(job.get("if") or "").strip() != MAIN_ONLY_IF:
            issues.append(f"{name}: job {job_id} reads release signing secrets and must run only if: {MAIN_ONLY_IF}")
    return issues


def _step_env_value(workflow: dict[str, Any], job: dict[str, Any], step: dict[str, Any], key: str) -> str:
    for scope in (step, job, workflow):
        env = scope.get("env") if isinstance(scope, dict) else None
        if isinstance(env, dict) and key in env:
            return str(env[key])
    return ""


def _verifier_issues(name: str, text: str, workflow: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if "OMNIDESK_RELEASE_SIGNING_" in text:
        issues.append(f"{name}: release verifiers must not hold any release signing secret")
    checked = False
    for job in (workflow.get("jobs") or {}).values():
        for step in (job.get("steps") or []) if isinstance(job, dict) else []:
            if isinstance(step, dict) and "verify_release_signatures.py" in str(step.get("run") or ""):
                checked = True
                if _step_env_value(workflow, job, step, "OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES") != "1":
                    issues.append(f"{name}: verify_release_signatures.py must run with OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES=1")
    if not checked:
        issues.append(f"{name}: must verify Ed25519 release signatures with verify_release_signatures.py")
    return issues


def _attestation_ref_issues(name: str, text: str, workflow: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    env = workflow.get("env") if isinstance(workflow.get("env"), dict) else {}
    for line in text.splitlines():
        if "gh attestation verify" not in line or line.strip().startswith("#"):
            continue
        if "--signer-workflow" not in line:
            issues.append(f"{name}: gh attestation verify must pin --signer-workflow")
        if '--source-ref "$RELEASE_SOURCE_REF"' in line:
            if str(env.get("RELEASE_SOURCE_REF") or "") != "refs/heads/main":
                issues.append(f"{name}: RELEASE_SOURCE_REF must be refs/heads/main")
        elif "--source-ref refs/heads/main" not in line:
            issues.append(f"{name}: gh attestation verify must pin --source-ref refs/heads/main")
    for identity in SIGNER_IDENTITY_RE.findall(text):
        if not identity.endswith("release\\.yml@refs/heads/main$") or "tags" in identity:
            issues.append(f"{name}: RELEASE_SIGNER_IDENTITY_REGEXP must only accept release.yml@refs/heads/main")
    return issues


def _grants_attestation_write(permissions: Any) -> bool:
    if isinstance(permissions, str):
        return permissions.strip() == "write-all"
    return isinstance(permissions, dict) and str(permissions.get("attestations") or "") == "write"


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps") or [] if isinstance(step, dict)]


def _mints_attestations(job: dict[str, Any]) -> bool:
    if _grants_attestation_write(job.get("permissions")) or SIGNING_ACTION_RE.match(str(job.get("uses") or "")):
        return True
    for step in _job_steps(job):
        if SIGNING_ACTION_RE.match(str(step.get("uses") or "")) or SIGNING_COMMAND_RE.search(str(step.get("run") or "")):
            return True
    return False


def _workflow_events(workflow: dict[Any, Any]) -> set[str]:
    # PyYAML reads the bare key `on` as True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(event) for event in on}
    return set()


def _unchecked_run_download(step: dict[str, Any], producers: tuple[str, ...]) -> bool:
    with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
    if str(step.get("uses") or "").lower().startswith("actions/download-artifact@") and "run-id" in with_:
        return True
    # Each download needs its own run check earlier in the same step.
    chunks = str(step.get("run") or "").split("gh run download")
    return any(
        not all(snippet in before for snippet in SOURCE_RUN_CHECK_SNIPPETS)
        or not any(f"'path': '{producer}'" in before for producer in producers)
        for before in chunks[:-1]
    )


def _attestation_signer_issues(name: str, workflow: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if _grants_attestation_write(workflow.get("permissions")):
        issues.append(f"{name}: attestations: write must be granted only to a registered signer job, not workflow-wide")
    signer_jobs = ATTESTATION_SIGNER_JOBS.get(name, {})
    events = _workflow_events(workflow)
    for job_id, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if _mints_attestations(job) and job_id not in signer_jobs:
            issues.append(
                f"{name}: job {job_id} mints artifact attestations or Sigstore signatures but is not a registered "
                "signer job (ATTESTATION_SIGNER_JOBS in scripts/check_security_workflow_policy.py)"
            )
        elif job_id in signer_jobs:
            if str(job.get("if") or "").strip() != MAIN_ONLY_IF:
                issues.append(f"{name}: signer job {job_id} must run only if: {MAIN_ONLY_IF}")
            if _job_environment(job) != "release":
                issues.append(f"{name}: signer job {job_id} must run in the protected release environment")
            if not events or not events <= set(ATTESTATION_SIGNER_EVENTS):
                issues.append(
                    f"{name}: signer workflow may only be triggered by {', '.join(ATTESTATION_SIGNER_EVENTS)}, not {sorted(events)}"
                )
        # Any job of a signer workflow can feed the signer job (needs + artifacts).
        if signer_jobs and any(_unchecked_run_download(step, signer_jobs.get(job_id, ())) for step in _job_steps(job)):
            issues.append(
                f"{name}: job {job_id} downloads artifacts from a run id without first checking that run "
                "(head_branch main, head_sha, success, event, registered producer path)"
            )
    return issues


def _evidence_source_issues(root: Path) -> list[str]:
    issues: list[str] = []
    policy_path = root / EVIDENCE_SOURCE_POLICY
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"missing or invalid evidence source policy: {EVIDENCE_SOURCE_POLICY}"]
    consumers = policy.get("consumers") if isinstance(policy, dict) else None
    if not isinstance(consumers, dict):
        return [f"{EVIDENCE_SOURCE_POLICY}: consumers are required"]
    for workflow_name, consumer in EVIDENCE_CONSUMER_WORKFLOWS.items():
        spec = consumers.get(consumer)
        if not isinstance(spec, dict):
            issues.append(f"{EVIDENCE_SOURCE_POLICY}: missing consumer {consumer}")
            continue
        if spec.get("head_branch") != "main":
            issues.append(f"{EVIDENCE_SOURCE_POLICY}: consumer {consumer} must require head_branch main")
        if spec.get("head_sha") not in {"exact", "ancestor"}:
            issues.append(f"{EVIDENCE_SOURCE_POLICY}: consumer {consumer} must set head_sha to exact or ancestor")
        if "pull_request" in (spec.get("events") or []) or "pull_request_target" in (spec.get("events") or []):
            issues.append(f"{EVIDENCE_SOURCE_POLICY}: consumer {consumer} must not accept pull request runs")
        for producer in spec.get("workflows") or []:
            if not str(producer).startswith(".github/workflows/") or not (root / str(producer)).is_file():
                issues.append(f"{EVIDENCE_SOURCE_POLICY}: consumer {consumer} lists unknown producer workflow {producer}")
        workflow_path = root / ".github" / "workflows" / workflow_name
        text = workflow_path.read_text(encoding="utf-8") if workflow_path.exists() else ""
        if not text:
            continue
        if "gh run download" in text:
            issues.append(f"{workflow_name}: evidence must be downloaded with scripts/fetch_ci_evidence_artifact.py, not gh run download")
        if "scripts/fetch_ci_evidence_artifact.py" not in text or f"--consumer {consumer}" not in text:
            issues.append(f"{workflow_name}: evidence downloads must use scripts/fetch_ci_evidence_artifact.py --consumer {consumer}")
    for consumer, spec in consumers.items():
        if isinstance(spec, dict) and spec.get("allow_same_run") and consumer not in SAME_RUN_EVIDENCE_CONSUMERS:
            issues.append(f"{EVIDENCE_SOURCE_POLICY}: consumer {consumer} must not skip the source run check (allow_same_run)")
    if consumers.get("release", {}).get("head_sha") != "exact":
        issues.append(f"{EVIDENCE_SOURCE_POLICY}: release must only accept evidence bound to the exact release commit")
    return issues


def release_chain_issues(root: Path) -> list[str]:
    """Release signing, attestation and evidence-import rules across all workflows."""
    issues = _pytest_collection_issues(root)
    workflow_dir = root / ".github" / "workflows"
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        workflow = _load_workflow(path)
        for snippet in FORBIDDEN_WORKFLOW_SNIPPETS:
            if snippet in text:
                issues.append(f"{path.name}: must not use {snippet}")
        issues.extend(_signing_job_scope_issues(path.name, workflow))
        issues.extend(_attestation_ref_issues(path.name, text, workflow))
        issues.extend(_attestation_signer_issues(path.name, workflow))
        if path.name in RELEASE_VERIFIER_WORKFLOWS:
            issues.extend(_verifier_issues(path.name, text, workflow))
    release = workflow_dir / "release.yml"
    if release.exists():
        jobs = _load_workflow(release).get("jobs") or {}
        build = jobs.get("build") if isinstance(jobs, dict) else None
        if not isinstance(build, dict) or _job_environment(build) != "release" or str(build.get("if") or "").strip() != MAIN_ONLY_IF:
            issues.append(f"release.yml: build job must run only if: {MAIN_ONLY_IF} in the release environment")
        if "refs/tags/" in release.read_text(encoding="utf-8"):
            issues.append("release.yml: release tags are not a trusted signing ref")
    issues.extend(_evidence_source_issues(root))
    return issues


def _codeql_pin_issues(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"github/codeql-action/[a-z-]+@(?![0-9a-f]{40}\b)", text):
        return ["security.yml: github/codeql-action must be pinned to a full commit SHA"]
    pins: dict[str, set[str]] = {}
    for step, sha in CODEQL_PIN_RE.findall(text):
        pins.setdefault(step, set()).add(sha)
    if not pins.get("init") or not pins.get("analyze"):
        return ["security.yml: github/codeql-action init and analyze steps are required"]
    if len(pins["init"] | pins["analyze"]) != 1:
        return ["security.yml: github/codeql-action init and analyze must use the same commit"]
    return []


def _missing_snippets(path: Path, snippets: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [snippet for snippet in snippets if snippet not in text]


def _audit_loop_contains_lockfile(security_text: str, lockfile: str) -> bool:
    audit_start = security_text.find("- name: Audit Python lock files")
    audit_end = security_text.find("- run: python scripts/check_license_policy.py", audit_start)
    if audit_start < 0 or audit_end < 0:
        return False
    return lockfile in security_text[audit_start:audit_end]


def check(root: Path) -> list[str]:
    issues: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            issues.append(f"missing security policy asset: {rel}")
    security_workflow = root / ".github" / "workflows" / "security.yml"
    missing_security = _missing_snippets(security_workflow, SECURITY_SNIPPETS)
    if missing_security:
        issues.append("security.yml missing snippets: " + ", ".join(missing_security))
    issues.extend(_codeql_pin_issues(security_workflow))
    attack_surface_workflow = root / ".github" / "workflows" / "security-attack-surface.yml"
    missing_attack_surface = _missing_snippets(
        attack_surface_workflow, ATTACK_SURFACE_WORKFLOW_SNIPPETS
    )
    if missing_attack_surface:
        issues.append(
            "security-attack-surface.yml missing snippets: "
            + ", ".join(missing_attack_surface)
        )
    security_text = security_workflow.read_text(encoding="utf-8") if security_workflow.exists() else ""
    for lockfile in LOCKFILES:
        if f"python scripts/check_lock_hashes.py {lockfile}" not in security_text:
            issues.append(f"security.yml must hash-check lockfile: {lockfile}")
        if not _audit_loop_contains_lockfile(security_text, lockfile):
            issues.append(f"security.yml must pip-audit lockfile: {lockfile}")
    if "pip-audit-${stem}.status" not in security_text:
        issues.append("security.yml must preserve per-lockfile pip-audit status evidence")
    if 'echo "status=$aggregate" >> "$GITHUB_OUTPUT"' not in security_text:
        issues.append("security.yml must aggregate Python audit results before enforcement")
    if 'echo "status=$status" >> "$GITHUB_OUTPUT"' not in security_text:
        issues.append("security.yml must preserve ecosystem audit step status")
    dependency_review_index = security_text.find("actions/dependency-review-action@")
    if dependency_review_index >= 0:
        dependency_review_block = security_text[
            dependency_review_index : dependency_review_index + 300
        ]
        if "continue-on-error: true" in dependency_review_block:
            issues.append(
                "dependency-review must be blocking; remove continue-on-error: true"
            )
    allow_ghsa_lines = [
        line.strip()
        for line in security_text.splitlines()
        if line.strip().startswith("allow-ghsas:")
    ]
    if allow_ghsa_lines or "--ignore" in security_text:
        issues.append(
            "dependency-review and ecosystem audits must not suppress advisories"
        )
    if "check_security_exceptions.py" not in security_text:
        issues.append("security.yml must validate governed security exceptions")
    if "Run gitleaks full-history secret scan" not in security_text:
        issues.append("security.yml must include a full-history gitleaks job")
    missing_docker = _missing_snippets(
        root / ".github" / "workflows" / "docker-scan.yml", DOCKER_SCAN_SNIPPETS
    )
    if missing_docker:
        issues.append("docker-scan.yml missing snippets: " + ", ".join(missing_docker))
    issues.extend(release_chain_issues(root))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify security workflows include CodeQL, secret scanning, dependency review, "
            "license policy, fail-closed cross-ecosystem dependency audits, container scanning, "
            "attack-surface gates, and release signing/attestation/evidence-import rules."
        )
    )
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    issues = check(Path(args.root))
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("security workflow policy verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
