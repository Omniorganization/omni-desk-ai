from pathlib import Path


def test_browserstack_artifact_uploads_external_evidence_root():
    workflow = Path(".github/workflows/browserstack-android-evidence.yml").read_text(
        encoding="utf-8"
    )

    assert "name: external-ga-evidence-raw" in workflow
    assert "path: dist/browserstack-evidence/release/external-evidence" in workflow


def test_real_ga_readiness_normalizes_downloaded_evidence_and_passes_token():
    workflow = Path(".github/workflows/real-ga-readiness.yml").read_text(
        encoding="utf-8"
    )

    assert "--dir dist/imported-external-evidence" in workflow
    assert 'raw_root / "release" / "external-evidence"' in workflow
    assert "release/external-evidence" in workflow
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow
