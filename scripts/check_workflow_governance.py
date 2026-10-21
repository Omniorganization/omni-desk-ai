#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_SNIPPETS = [
    "--scope web-admin",
    "--scope desktop",
    "--scope mobile",
    "--scope tri-app",
    "--scope external-ga-evidence",
    "--scope ios-evidence",
    "--scope tri-app-live-smoke",
    "EXTERNAL_GA_EVIDENCE_EXPECTED_VERSION",
    "import_external_ga_evidence.py",
    "IOS_EVIDENCE_EXPECTED_VERSION",
    "import_ios_real_device_evidence.py",
    "import_tri_app_live_smoke_evidence.py",
    "real-ga-evidence-audit-1.12.7.json",
    "external-ga-evidence-import-report.json",
    "tri-app-live-smoke-evidence-import-report.json",
]

REPORT_UPLOAD_SNIPPETS = [
    "upload-artifact",
    "dist/",
    "release/real-ga-evidence-audit-1.12.7.json",
    "release/external-ga-evidence-import-report.json",
    "release/ios-real-device-evidence-import-report.json",
    "release/tri-app-live-smoke-evidence-import-report.json",
]


# Every file that ships in dist/ must exist before checksums.txt and the
# Ed25519 signatures are written, and GitHub attestation comes last.
RELEASE_PAYLOAD_ORDER = [
    "bash scripts/build_release.sh",
    "--output dist/tri-app-native-artifacts.json",
    "scripts/render_locked_helm_values.py",
    "scripts/write_slsa_provenance.py",
    "cosign sign-blob",
    "cosign attest-blob",
    "scripts/finalize_release_payload.py dist",
    "actions/attest-build-provenance@",
]

# Gate reports live in reports/release/, outside the signed payload: build_release.sh
# starts with rm -rf dist, so a report written to dist/ before it is silently lost.
RELEASE_REPORTS_ARTIFACT = "name: real-ga-evidence-import-reports"
RELEASE_REPORTS_CHECK_MARKER = "missing Real GA evidence import report"
RELEASE_GATE_REPORTS = [
    "reports/release/web-admin-preflight.json",
    "reports/release/desktop-preflight.json",
    "reports/release/mobile-preflight.json",
    "reports/release/tri-app-preflight.json",
    "reports/release/external-ga-evidence-preflight.json",
    "reports/release/ios-evidence-preflight.json",
    "reports/release/tri-app-live-smoke-preflight.json",
    "reports/release/current-release-artifact-binding.json",
    "reports/release/main-verification-live-artifact.json",
    "reports/release/external-ga-evidence-audit.json",
    "reports/release/external-ga-evidence-summary.json",
]
# A workspace-relative dist/ path (not apps/x/dist/ or $RUNNER_TEMP/dist/).
DIST_PATH_RE = re.compile(r"(?<![\w./$}-])dist/")
JOB_KEY_RE = re.compile(r"^  [\w-]+:[ \t]*$", re.MULTILINE)

RELEASE_CHAIN_SNIPPETS = [
    'OMNIDESK_RELEASE_DEFER_SIGNING: "1"',
    "OMNIDESK_RELEASE_SIGNING_ED25519_KEY: ${{ secrets.OMNIDESK_RELEASE_SIGNING_ED25519_KEY }}",
    'OMNIDESK_REQUIRE_ED25519_RELEASE_SIGNATURES: "1"',
    "release/release-signing-public-key.pem",
    "scripts/fetch_ci_evidence_artifact.py",
    "--consumer release",
    "environment: release",
]


def _release_chain_issues(text: str) -> list[str]:
    issues = [
        f"release workflow missing release-chain snippet: {snippet}"
        for snippet in RELEASE_CHAIN_SNIPPETS
        if snippet not in text
    ]
    if "gh run download" in text:
        issues.append("release workflow must import evidence through scripts/fetch_ci_evidence_artifact.py, not gh run download")
    if "scripts/sign_release.py" in text:
        issues.append("release workflow must sign only through scripts/finalize_release_payload.py after the payload is complete")
    positions = []
    for snippet in RELEASE_PAYLOAD_ORDER:
        index = text.find(snippet)
        if index < 0:
            issues.append(f"release workflow missing payload step: {snippet}")
            return issues
        positions.append(index)
    for (before, after), (first, second) in zip(zip(RELEASE_PAYLOAD_ORDER, RELEASE_PAYLOAD_ORDER[1:]), zip(positions, positions[1:])):
        if first > second:
            issues.append(
                f"release workflow must run {before} before {after} so nothing is written to dist/ after signing"
            )
    finalize = positions[RELEASE_PAYLOAD_ORDER.index("scripts/finalize_release_payload.py dist")]
    attest = positions[-1]
    for writer in ("> dist/", "--output dist/", "-czf dist/", "--output-certificate dist/", "--output-signature dist/"):
        index = text.find(writer, finalize)
        if 0 <= index < attest:
            issues.append(f"release workflow writes to dist/ after signing: {writer.strip()}")
    return issues


def _step_containing(text: str, marker: str) -> str:
    """The workflow step (YAML list item) whose lines contain marker, or ""."""
    lines = text.splitlines()
    hit = next((i for i, line in enumerate(lines) if marker in line), None)
    if hit is None:
        return ""
    start = next((i for i in range(hit, -1, -1) if lines[i].lstrip().startswith("- ")), hit)
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= indent
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _release_report_issues(text: str) -> list[str]:
    issues: list[str] = []
    build = text.find("bash scripts/build_release.sh")
    if build < 0:
        return []  # _release_chain_issues reports the missing payload step.
    job_start = max((m.start() for m in JOB_KEY_RE.finditer(text, 0, build)), default=0)
    for match in DIST_PATH_RE.finditer(text, job_start, build):
        line = text[text.rfind("\n", 0, match.start()) + 1 : text.find("\n", match.start())].strip()
        if not line.startswith("#"):
            issues.append(
                "release workflow uses dist/ before bash scripts/build_release.sh, which deletes it; "
                f"write gate reports to reports/release/: {line}"
            )
            break
    # if-no-files-found only fails when none of the upload's files exist, so a
    # step before the build (and before anything is published) checks each report.
    check_step = _step_containing(text, RELEASE_REPORTS_CHECK_MARKER)
    if not 0 <= text.find(RELEASE_REPORTS_CHECK_MARKER) < build:
        issues.append("release workflow must check every gate report exists before bash scripts/build_release.sh")
    upload = _step_containing(text, RELEASE_REPORTS_ARTIFACT)
    if not upload:
        issues.append("release workflow must upload gate reports as the real-ga-evidence-import-reports artifact")
        return issues
    if "if-no-files-found: error" not in upload:
        issues.append("real-ga-evidence-import-reports upload must set if-no-files-found: error")
    if DIST_PATH_RE.search(upload):
        issues.append("real-ga-evidence-import-reports must not upload from dist/, the signed release payload")
    for report in RELEASE_GATE_REPORTS:
        if report not in check_step:
            issues.append(f"release workflow report check missing {report}")
        if report not in upload:
            issues.append(f"real-ga-evidence-import-reports upload missing {report}")
    return issues


def _read_existing(paths: list[Path]) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in paths if p.exists() and p.is_file()
    )


def check(root: Path, *, require_real_workflows: bool = False) -> list[str]:
    issues: list[str] = []
    workflow_paths = sorted((root / ".github" / "workflows").glob("*.yml")) + sorted(
        (root / ".github" / "workflows").glob("*.yaml")
    )
    release_workflow = root / ".github" / "workflows" / "release.yml"
    fallback_paths = [root / "patches" / "v1.12.7-apply.patch", root / "Makefile"]
    if require_real_workflows:
        if not release_workflow.exists():
            return ["real workflow mode requires .github/workflows/release.yml"]
        text = _read_existing([release_workflow])
    else:
        text = _read_existing(workflow_paths + fallback_paths)
    if not text:
        issues.append(
            "no workflow, Makefile, or v1.12.7 patch content found for governance validation"
        )
        return issues
    for snippet in REQUIRED_SNIPPETS:
        if snippet not in text:
            issues.append(f"workflow governance missing required snippet: {snippet}")
    # Upload-artifact evidence is required in real workflows.
    if workflow_paths or require_real_workflows:
        for snippet in REPORT_UPLOAD_SNIPPETS:
            if snippet not in text:
                issues.append(f"workflow evidence upload missing snippet: {snippet}")
    if require_real_workflows:
        for snippet in ("release_metadata", "attestation", "write_slsa_provenance.py"):
            if snippet not in text:
                issues.append(
                    f"real workflow governance missing release metadata/attestation snippet: {snippet}"
                )
        issues.extend(_release_chain_issues(text))
        issues.extend(_release_report_issues(text))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify release workflows wire tri-app and real-device GA evidence gates."
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--require-real-workflows", action="store_true")
    args = parser.parse_args(argv)
    issues = check(Path(args.root), require_real_workflows=args.require_real_workflows)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("workflow governance contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
