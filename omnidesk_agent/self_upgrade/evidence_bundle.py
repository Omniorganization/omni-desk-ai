from __future__ import annotations

import hashlib
import json
from pathlib import Path

from omnidesk_agent.repair_contracts import RepairEvidenceBundle

EvidenceBundle = RepairEvidenceBundle


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_bundle(
    *,
    incident_id: str,
    branch: str,
    tests: tuple[str, ...],
    gates: tuple[str, ...],
    rollback_plan: str,
    artifacts: tuple[Path, ...] = (),
) -> EvidenceBundle:
    hashes = {str(path): sha256_file(path) for path in artifacts if path.exists()}
    return EvidenceBundle(
        incident_id=incident_id,
        branch=branch,
        tests=tests,
        gates=gates,
        rollback_plan=rollback_plan,
        artifacts=tuple(str(path) for path in artifacts),
        artifact_hashes=hashes,
    )


def write_evidence_bundle(bundle: EvidenceBundle, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path
