from pathlib import Path


def test_browserstack_artifact_uploads_external_evidence_root():
    workflow = Path(".github/workflows/browserstack-android-evidence.yml").read_text(
        encoding="utf-8"
    )

    assert "name: external-ga-evidence-raw" in workflow
    assert "path: dist/browserstack-evidence/release/external-evidence" in workflow


def test_real_ga_readiness_imports_evidence_through_fetch_helper_and_passes_token():
    workflow = Path(".github/workflows/real-ga-readiness.yml").read_text(
        encoding="utf-8"
    )

    # The helper unpacks nested release/external-evidence bundles itself and copies
    # only the expected evidence files.
    assert "gh run download" not in workflow
    assert "--consumer real-ga-readiness" in workflow
    assert "--dest release/external-evidence" in workflow
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow
