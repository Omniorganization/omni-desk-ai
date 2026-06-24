from __future__ import annotations

import json
from pathlib import Path

from scripts.write_distribution_manifest import main
from scripts.write_portable_sha256s import main as write_sha256s_main


VERSION = "1.12.7+root-monorepo-production-ga-candidate"
SLUG = "Omni-desk-AI-1.12.7-root-monorepo-production-ga-candidate"


def _write_required_artifacts(package_dir: Path) -> None:
    files = [
        "Omni-desk-AI-1.12.7-core-release.zip",
        "Omni-desk-AI-1.12.7-web-admin.zip",
        "Omni-desk-AI-1.12.7-desktop.zip",
        "Omni-desk-AI-1.12.7-mobile.zip",
        f"{SLUG}-full.zip",
    ]
    for item in files:
        (package_dir / item).write_bytes(f"artifact:{item}".encode("utf-8"))
    assert write_sha256s_main(["--base-dir", str(package_dir), "--output", "SHA256SUMS.txt", *files]) == 0


def test_distribution_manifest_records_blocked_external_ga_status(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    _write_required_artifacts(package_dir)
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "status": "blocked_missing_external_evidence",
                "blocker_count": 1,
                "policy": "real external systems required",
                "categories": {
                    "signed_artifacts": {
                        "ok": False,
                        "label": "true signed artifacts",
                        "issues": ["missing signed IPA"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--package-dir",
                str(package_dir),
                "--version",
                VERSION,
                "--package-slug",
                SLUG,
                "--source-commit",
                "abc123",
                "--external-audit",
                str(audit),
            ]
        )
        == 0
    )
    assert main(["--package-dir", str(package_dir), "--verify"]) == 0
    manifest = json.loads((package_dir / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["release_status"] == "source_gated_production_ga_candidate"
    assert manifest["source_commit"] == "abc123"
    assert manifest["external_ga_evidence"]["blocking_categories"][0]["category"] == "signed_artifacts"


def test_distribution_manifest_rejects_missing_required_artifact(tmp_path: Path, capsys) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "Omni-desk-AI-1.12.7-core-release.zip").write_bytes(b"core")
    assert (
        main(
            [
                "--package-dir",
                str(package_dir),
                "--version",
                VERSION,
                "--package-slug",
                SLUG,
            ]
        )
        == 1
    )
    assert "missing required distribution artifacts" in capsys.readouterr().err
