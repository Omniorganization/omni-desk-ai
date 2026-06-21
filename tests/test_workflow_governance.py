from __future__ import annotations

from scripts.check_workflow_governance import main


def test_workflow_governance_accepts_patch_contract(tmp_path, capsys) -> None:
    patch = tmp_path / "patches" / "v1.12.5-apply.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("""
python scripts/check_release_configuration.py --scope web-admin
python scripts/check_release_configuration.py --scope desktop
python scripts/check_release_configuration.py --scope mobile
python scripts/check_release_configuration.py --scope tri-app
python scripts/check_release_configuration.py --scope ios-evidence
python scripts/check_release_configuration.py --scope tri-app-live-smoke
IOS_EVIDENCE_EXPECTED_VERSION=1.12.5+root-monorepo-production-ga-candidate
python scripts/import_ios_real_device_evidence.py --write-report release/real-ga-evidence-audit-1.12.5.json
python scripts/import_tri_app_live_smoke_evidence.py --write-report release/tri-app-live-smoke-evidence-import-report.json
actions/upload-artifact
dist/
release/ios-real-device-evidence-import-report.json
release/tri-app-live-smoke-evidence-import-report.json
release_metadata
attestation
write_slsa_provenance.py
""", encoding="utf-8")

    assert main([str(tmp_path)]) == 0
    assert "workflow governance contract verified" in capsys.readouterr().out


def test_workflow_governance_rejects_missing_tri_app_scope(tmp_path, capsys) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "release.yml").write_text("""
name: release
steps:
  - run: python scripts/check_release_configuration.py --scope web-admin
""", encoding="utf-8")

    assert main([str(tmp_path)]) == 1
    assert "--scope tri-app" in capsys.readouterr().err


def test_workflow_governance_real_workflow_mode_rejects_patch_only(tmp_path, capsys) -> None:
    patch = tmp_path / "patches" / "v1.12.5-apply.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("""
python scripts/check_release_configuration.py --scope web-admin
python scripts/check_release_configuration.py --scope desktop
python scripts/check_release_configuration.py --scope mobile
python scripts/check_release_configuration.py --scope tri-app
python scripts/check_release_configuration.py --scope ios-evidence
python scripts/check_release_configuration.py --scope tri-app-live-smoke
IOS_EVIDENCE_EXPECTED_VERSION=1.12.5+root-monorepo-production-ga-candidate
python scripts/import_ios_real_device_evidence.py --write-report release/real-ga-evidence-audit-1.12.5.json
python scripts/import_tri_app_live_smoke_evidence.py --write-report release/tri-app-live-smoke-evidence-import-report.json
""", encoding="utf-8")

    assert main([str(tmp_path), "--require-real-workflows"]) == 1
    assert "real workflow mode requires .github/workflows/release.yml" in capsys.readouterr().err


def test_workflow_governance_real_workflow_mode_accepts_release_workflow(tmp_path, capsys) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "release.yml").write_text("""
name: release
steps:
  - run: python scripts/check_release_configuration.py --scope web-admin
  - run: python scripts/check_release_configuration.py --scope desktop
  - run: python scripts/check_release_configuration.py --scope mobile
  - run: python scripts/check_release_configuration.py --scope tri-app
  - env:
      IOS_EVIDENCE_EXPECTED_VERSION: 1.12.5+root-monorepo-production-ga-candidate
  - run: python scripts/check_release_configuration.py --scope ios-evidence
  - run: python scripts/check_release_configuration.py --scope tri-app-live-smoke
  - run: python scripts/import_ios_real_device_evidence.py --write-report release/real-ga-evidence-audit-1.12.5.json
  - run: python scripts/import_tri_app_live_smoke_evidence.py --write-report release/tri-app-live-smoke-evidence-import-report.json
  - uses: actions/upload-artifact
    with:
      path: |
        dist/
        release/real-ga-evidence-audit-1.12.5.json
        release/ios-real-device-evidence-import-report.json
        release/tri-app-live-smoke-evidence-import-report.json
  - run: python scripts/write_slsa_provenance.py dist --builder-id release_metadata
  - run: gh attestation sign dist/release_metadata.json
""", encoding="utf-8")

    assert main([str(tmp_path), "--require-real-workflows"]) == 0
    assert "workflow governance contract verified" in capsys.readouterr().out
