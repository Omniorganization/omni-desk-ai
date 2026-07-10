#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_SUFFIXES = (".cosign.sig", ".cosign.pem", ".intoto.sig", ".intoto.pem")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def build_manifest(
    platform: str,
    artifact_root: Path,
    source_commit: str,
    output: Path | None = None,
    *,
    build_run_id: str = "",
    artifact_attestation: dict[str, str] | None = None,
) -> dict:
    root = artifact_root.resolve()
    excluded = output.resolve() if output else None
    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.resolve() != excluded
        and not path.name.endswith(EXCLUDED_SUFFIXES)
    ]
    if not files:
        raise RuntimeError(f"no release artifacts found under {artifact_root}")
    artifacts = [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "release_payload_artifact_sha256": sha256(path),
        }
        for path in files
    ]
    report = {
        "schema": "omnidesk-native-artifact-set/v1",
        "status": "passed",
        "producer": "Release Build",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "source_commit": source_commit,
        "build_run_id": build_run_id or os.environ.get("GITHUB_RUN_ID", "").strip(),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    if len(artifacts) == 1:
        report["release_payload_artifact_sha256"] = artifacts[0]["sha256"]
    else:
        report["release_payload_artifact_sha256_by_path"] = {
            item["path"]: item["sha256"] for item in artifacts
        }
    if artifact_attestation:
        report["artifact_attestation"] = artifact_attestation
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a digest-bound native release artifact manifest.")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--build-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--artifact-attestation-id", default="")
    parser.add_argument("--artifact-attestation-subject-sha256", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_attestation = None
    if args.artifact_attestation_id or args.artifact_attestation_subject_sha256:
        if not args.artifact_attestation_id or not args.artifact_attestation_subject_sha256:
            raise SystemExit("artifact attestation id and subject sha256 must be supplied together")
        artifact_attestation = {
            "attestation_id": args.artifact_attestation_id,
            "subject_sha256": args.artifact_attestation_subject_sha256,
        }
    report = build_manifest(
        args.platform,
        Path(args.artifact_root),
        args.source_commit,
        output,
        build_run_id=args.build_run_id,
        artifact_attestation=artifact_attestation,
    )
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"platform": args.platform, "artifact_count": report["artifact_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
