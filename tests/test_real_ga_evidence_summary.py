from __future__ import annotations

import json
from pathlib import Path

from scripts import write_real_ga_evidence_summary


def test_real_ga_evidence_summary_preserves_blockers_without_claiming_real_ga(tmp_path: Path) -> None:
    root = tmp_path
    (root / "release").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "omnidesk-agent"\nversion = "1.12.5+root-monorepo-production-ga-candidate"\n',
        encoding="utf-8",
    )
    audit = {
        "version": "1.12.5+root-monorepo-production-ga-candidate",
        "status": "blocked_missing_external_evidence",
        "blocker_count": 1,
        "policy": "real external systems required",
        "categories": {
            "postgres_soak": {
                "label": "true multi-instance Postgres soak",
                "ok": False,
                "issues": ["missing evidence file: drills/postgres-multi-instance-soak.json"],
                "files": [{"path": "drills/postgres-multi-instance-soak.json", "ok": False, "issues": ["missing"]}],
            },
            "native_build": {
                "label": "true Flutter/Rust/Tauri native build",
                "ok": True,
                "issues": [],
                "files": [{"path": "native-build/flutter-android-release.json", "ok": True, "issues": []}],
            },
        },
    }
    audit_path = root / "release" / "real-ga-evidence-audit-1.12.5.json"
    output_path = root / "release" / "real-ga-evidence-summary-1.12.5.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    rc = write_real_ga_evidence_summary.main(
        [
            str(root),
            "--audit-report",
            str(audit_path),
            "--output",
            str(output_path),
            "--source-commit",
            "abc123",
        ]
    )

    assert rc == 0
    summary = json.loads(output_path.read_text(encoding="utf-8"))
    assert summary["schema_version"] == "omnidesk-real-ga-evidence-summary/v1"
    assert summary["source_commit"] == "abc123"
    assert summary["real_ga_ready"] is False
    assert summary["blocker_count"] == 1
    assert summary["blocking_categories"] == [
        {
            "category": "postgres_soak",
            "label": "true multi-instance Postgres soak",
            "failed_file_count": 1,
            "issue_count": 1,
        }
    ]
