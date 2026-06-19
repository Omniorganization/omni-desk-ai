#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _subject_for(path: Path) -> dict:
    return {"name": path.name, "digest": {"sha256": _sha256(path)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an SLSA/in-toto provenance predicate for the complete release artifact set.")
    parser.add_argument("dist_dir", nargs="?", default="dist")
    parser.add_argument("--builder-id", default=os.getenv("GITHUB_WORKFLOW", "local-builder"))
    parser.add_argument("--source-ref", default=os.getenv("GITHUB_REF", "local"))
    parser.add_argument("--image-ref", default=os.getenv("OMNIDESK_IMAGE_REF", ""))
    parser.add_argument("--image-digest", default=os.getenv("OMNIDESK_IMAGE_DIGEST", ""))
    args = parser.parse_args(argv)
    root = Path(args.dist_dir)
    if not root.exists():
        raise SystemExit(f"missing dist dir: {root}")
    subjects = []
    for artifact in sorted(root.iterdir()):
        if artifact.is_file() and artifact.name != "slsa-provenance.json" and not artifact.name.endswith((".cosign.sig", ".cosign.pem", ".intoto.sig", ".intoto.pem")):
            subjects.append(_subject_for(artifact))
    if args.image_digest:
        subjects.append({"name": args.image_ref or "oci-image", "digest": {"sha256": args.image_digest.removeprefix("sha256:")}})
    lockfiles = []
    for rel in (
        "requirements.lock",
        "requirements.bootstrap.lock",
        "requirements.runtime.lock",
        "requirements.dev.lock",
        "requirements.security.lock",
        "requirements.enterprise.lock",
    ):
        path = Path(rel)
        if path.exists():
            lockfiles.append({"name": rel, "digest": {"sha256": _sha256(path)}})
    predicate = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/actions/workflow/v1",
                "externalParameters": {
                    "source_ref": args.source_ref,
                    "image_ref": args.image_ref,
                    "image_digest": args.image_digest,
                },
                "resolvedDependencies": lockfiles,
            },
            "runDetails": {
                "builder": {"id": args.builder_id},
                "metadata": {
                    "invocationId": os.getenv("GITHUB_RUN_ID", "local"),
                    "startedOn": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "workflow_sha": os.getenv("GITHUB_SHA", "local"),
                    "workflow_repository": os.getenv("GITHUB_REPOSITORY", "local"),
                },
            },
        },
    }
    (root / "slsa-provenance.json").write_text(json.dumps(predicate, indent=2, sort_keys=True), encoding="utf-8")
    print(root / "slsa-provenance.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
