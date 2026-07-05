from __future__ import annotations

import runpy


def test_audit_core_boundary_tracks_audit_evidence_assets() -> None:
    boundary = runpy.run_path(
        "packages/audit-core/src/omnidesk_audit_core/boundary.py"
    )

    assert boundary["BOUNDARY_NAME"] == "audit-core"
    assert "omnidesk_agent/security/audit_worm.py" in boundary["OWNED_SOURCE_PATHS"]
    assert (
        "release/production-evidence.manifest.json" in boundary["OWNED_SOURCE_PATHS"]
    )
