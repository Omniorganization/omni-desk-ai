from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts import check_external_ga_evidence


def test_external_ga_evidence_gate_blocks_missing_real_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy("pyproject.toml", root / "pyproject.toml")

    report = check_external_ga_evidence.audit(root, root / "release" / "external-evidence")

    assert report["status"] == "blocked_missing_external_evidence"
    assert report["blocker_count"] == len(check_external_ga_evidence.REQUIRED_EVIDENCE)
    assert "native_build" in report["categories"]
    assert "web_admin_signed_oci_image" in report["categories"]
    assert "self_healing_failure_injection" in report["categories"]


def test_external_ga_evidence_script_is_wired_to_makefile_and_ga_gate() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    ga_gate = Path("scripts/check_ga_release_gate.py").read_text(encoding="utf-8")
    manifest = json.loads(Path("release/production-evidence.manifest.json").read_text(encoding="utf-8"))

    assert "external-ga-evidence-audit" in makefile
    assert "external-ga-evidence-gate" in makefile
    assert "check_external_ga_evidence.py" in ga_gate
    assert manifest["local_source_evidence"]["external_evidence_audit"] == "required"
    assert manifest["local_source_evidence"]["external_evidence_doctor"] == "required"
    assert manifest["local_source_evidence"]["config_profile_validation"] == "required"
