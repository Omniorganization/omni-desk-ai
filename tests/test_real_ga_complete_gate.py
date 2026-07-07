from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import check_real_ga_complete, check_team_governance_contract


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _team_contract(root: Path, *, owner_type: str = "User") -> None:
    _write_json(
        root / ".github" / "team-governance.required.json",
        {
            "schema": "omnidesk-team-governance/v1",
            "required_for_customer_distribution_ga": True,
            "current_repository_owner_type": owner_type,
            "required_repository_owner_type": "Organization",
            "personal_owner_fallback_forbidden_for_real_ga": True,
            "codeowners_file": ".github/CODEOWNERS",
            "migration_blocker": "Repository must move to an organization.",
            "required_organization": "omnidesk-ai",
            "required_teams": [
                {"slug": "omnidesk-maintainers", "required_paths": ["*"]},
                {"slug": "release-owners", "required_paths": [".github/workflows/", "scripts/", "release/", "omnidesk_agent/self_upgrade/"]},
                {"slug": "security-owners", "required_paths": [".github/workflows/", "release/", "omnidesk_agent/security/", "omnidesk_agent/sandbox/", "omnidesk_agent/plugins/", "omnidesk_agent/self_upgrade/"]},
                {"slug": "platform-owners", "required_paths": ["deploy/", "omnidesk_agent/sandbox/"]},
            ],
            "live_evidence_required_file": "release/external-evidence/control-plane/github-team-governance-live.json",
        },
    )


def test_team_governance_live_report_must_match_scope_and_required_teams(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    evidence = root / "release" / "external-evidence"
    root.mkdir()
    _team_contract(root)
    monkeypatch.setenv("GITHUB_REPOSITORY", "yinyufan0813-cmyk/omni-desk-ai")
    monkeypatch.setenv("GITHUB_SHA", "abc123")

    _write_json(
        evidence / "control-plane" / "github-team-governance-live.json",
        {
            "schema": "omnidesk-team-governance-live/v1",
            "status": "passed",
            "produced_at": "2026-07-07T00:00:00Z",
            "producer": "live-github-check",
            "repository": "other/repo",
            "owner": "wrong-org",
            "owner_type": "Organization",
            "organization": "wrong-org",
            "codeowners_ref": "old-sha",
            "repository_is_organization_owned": True,
            "required_teams_resolved": True,
            "codeowners_team_owned": True,
            "branch_protection_requires_codeowners_review": True,
            "admins_enforced": True,
            "personal_owner_fallback_active": False,
            "required_teams": ["unrelated-a", "unrelated-b", "unrelated-c", "unrelated-d"],
            "failures": [],
        },
    )

    result = check_real_ga_complete._team_governance(root, evidence)

    assert result["ok"] is False
    assert "repository must be yinyufan0813-cmyk/omni-desk-ai" in result["issues"]
    assert "organization must be omnidesk-ai" in result["issues"]
    assert "codeowners_ref must match the checked commit" in result["issues"]
    assert any("required_teams must match source contract teams" in issue for issue in result["issues"])


def test_native_signed_binding_must_match_commit_digest_and_evidence_hashes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    evidence = root / "release" / "external-evidence"
    root.mkdir()
    monkeypatch.setenv("GITHUB_SHA", "abc123")

    native_rows = []
    for rel in check_real_ga_complete.NATIVE_BUILD_EVIDENCE_PATHS:
        path = evidence / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"native {rel}", encoding="utf-8")
        native_rows.append({"path": rel, "present": True, "sha256": _sha256(path)})

    signed_rows = []
    for rel in check_real_ga_complete.SIGNED_ARTIFACT_EVIDENCE_PATHS:
        path = evidence / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"signed {rel}", encoding="utf-8")
        signed_rows.append({"path": rel, "present": True, "sha256": _sha256(path)})
    signed_rows[0]["sha256"] = "sha256:" + ("0" * 64)

    main_verification = evidence / "control-plane" / "main-verification-evidence.json"
    main_verification.parent.mkdir(parents=True, exist_ok=True)
    main_verification.write_text('{"commit":"abc123"}\n', encoding="utf-8")

    _write_json(
        evidence / "control-plane" / "native-signed-artifact-binding.json",
        {
            "schema": "omnidesk-native-signed-artifact-binding/v1",
            "status": "passed",
            "produced_at": "2026-07-07T00:00:00Z",
            "producer": "Main Verification",
            "main_verification_commit": "stale-sha",
            "main_verification_artifact_name": "main-verification-evidence-stale-sha",
            "main_verification_evidence_digest": "sha256:" + ("1" * 64),
            "real_ga_evidence_summary": "release/real-ga-evidence-summary-1.12.7.json",
            "native_builds_bound": True,
            "signed_artifacts_bound": True,
            "all_required_native_builds_present": True,
            "all_required_signed_artifacts_present": True,
            "native_build_evidence_paths": list(check_real_ga_complete.NATIVE_BUILD_EVIDENCE_PATHS),
            "signed_artifact_evidence_paths": list(check_real_ga_complete.SIGNED_ARTIFACT_EVIDENCE_PATHS),
            "native_build_evidence": native_rows,
            "signed_artifact_evidence": signed_rows,
        },
    )

    result = check_real_ga_complete._native_signed_binding(root, evidence)

    assert result["ok"] is False
    assert "main_verification_commit must match the checked commit" in result["issues"]
    assert "main_verification_artifact_name must be main-verification-evidence-abc123" in result["issues"]
    assert "main_verification_evidence_digest must match control-plane main-verification-evidence.json" in result["issues"]
    assert any("signed_artifact_evidence sha256 mismatch" in issue for issue in result["issues"])


def test_team_governance_contract_allows_organization_team_codeowners(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _team_contract(root, owner_type="Organization")
    (root / ".github" / "CODEOWNERS").write_text(
        "\n".join(
            [
                "* @omnidesk-ai/omnidesk-maintainers",
                ".github/workflows/ @omnidesk-ai/release-owners @omnidesk-ai/security-owners",
                "scripts/ @omnidesk-ai/release-owners",
                "deploy/ @omnidesk-ai/platform-owners",
                "release/ @omnidesk-ai/release-owners @omnidesk-ai/security-owners",
                "omnidesk_agent/security/ @omnidesk-ai/security-owners",
                "omnidesk_agent/sandbox/ @omnidesk-ai/security-owners @omnidesk-ai/platform-owners",
                "omnidesk_agent/plugins/ @omnidesk-ai/security-owners",
                "omnidesk_agent/self_upgrade/ @omnidesk-ai/release-owners @omnidesk-ai/security-owners",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert check_team_governance_contract.main([str(root)]) == 0
