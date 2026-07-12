#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind the Web Admin OCI artifact to its immutable Node base image."
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--node-base-name", required=True)
    parser.add_argument("--node-base-image", required=True)
    parser.add_argument("--node-base-digest", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    artifact = Path(args.artifact)
    if not artifact.is_file():
        raise SystemExit(f"missing Web Admin OCI artifact: {artifact}")
    if not DIGEST_RE.fullmatch(args.node_base_digest):
        raise SystemExit("node base digest must be sha256:<64 lowercase hex>")
    if "@" not in args.node_base_image:
        raise SystemExit("node base image must be immutable name@sha256:digest")
    if not args.node_base_image.endswith("@" + args.node_base_digest):
        raise SystemExit("node base image and digest disagree")
    if len(args.source_commit) != 40 or not all(
        char in "0123456789abcdef" for char in args.source_commit.lower()
    ):
        raise SystemExit("source commit must be a full Git SHA")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    produced_at = datetime.now(timezone.utc).isoformat()
    artifact_digest = "sha256:" + sha256(artifact)
    base_digest_hex = args.node_base_digest.removeprefix("sha256:")
    artifact_digest_hex = artifact_digest.removeprefix("sha256:")

    binding = {
        "schema": "omnidesk-web-supply-chain-binding/v1",
        "status": "passed",
        "produced_at": produced_at,
        "repository": args.repository,
        "source_commit": args.source_commit,
        "workflow_run_id": str(args.workflow_run_id),
        "image_ref": args.image_ref,
        "image_id": args.image_id,
        "node_base_name": args.node_base_name,
        "node_base_image": args.node_base_image,
        "node_base_digest": args.node_base_digest,
        "artifact": {
            "path": artifact.name,
            "size_bytes": artifact.stat().st_size,
            "sha256": artifact_digest,
        },
        "required_oci_labels": {
            "org.opencontainers.image.source": f"https://github.com/{args.repository}",
            "org.opencontainers.image.revision": args.source_commit,
            "org.opencontainers.image.base.name": args.node_base_name,
            "org.opencontainers.image.base.digest": args.node_base_digest,
            "ai.omnidesk.web.base.image": args.node_base_image,
        },
    }
    write_json(output_dir / "web-admin-supply-chain-binding.json", binding)

    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"omnidesk-web-admin-{args.source_commit[:12]}",
        "documentNamespace": (
            f"https://github.com/{args.repository}/actions/runs/"
            f"{args.workflow_run_id}/spdx/{args.source_commit}"
        ),
        "creationInfo": {
            "created": produced_at,
            "creators": ["Tool: scripts/write_web_supply_chain_binding.py"],
        },
        "packages": [
            {
                "name": "omnidesk-web-admin",
                "SPDXID": "SPDXRef-Package-WebAdmin",
                "versionInfo": args.source_commit,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": artifact_digest_hex}
                ],
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": (
                            f"pkg:oci/omnidesk-web-admin@{artifact_digest_hex}"
                            f"?repository_url={args.repository}"
                        ),
                    }
                ],
            },
            {
                "name": args.node_base_name,
                "SPDXID": "SPDXRef-Package-NodeBase",
                "versionInfo": args.node_base_digest,
                "downloadLocation": args.node_base_image,
                "filesAnalyzed": False,
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": base_digest_hex}
                ],
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": (
                            f"pkg:oci/{args.node_base_name.split(':', 1)[0]}"
                            f"@{base_digest_hex}"
                        ),
                    }
                ],
            },
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-Package-WebAdmin",
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": "SPDXRef-Package-NodeBase",
            },
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package-WebAdmin",
            },
        ],
    }
    write_json(output_dir / "web-admin-sbom.spdx.json", sbom)

    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": artifact.name,
                "digest": {"sha256": artifact_digest_hex},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/actions/workflow/v1",
                "externalParameters": {
                    "repository": args.repository,
                    "source_commit": args.source_commit,
                    "image_ref": args.image_ref,
                },
                "resolvedDependencies": [
                    {
                        "uri": args.node_base_image,
                        "digest": {"sha256": base_digest_hex},
                    },
                    {
                        "uri": f"git+https://github.com/{args.repository}@{args.source_commit}",
                        "digest": {"sha1": args.source_commit},
                    },
                ],
            },
            "runDetails": {
                "builder": {
                    "id": (
                        f"https://github.com/{args.repository}/actions/runs/"
                        f"{args.workflow_run_id}"
                    )
                },
                "metadata": {
                    "invocationId": str(args.workflow_run_id),
                    "startedOn": produced_at,
                },
            },
        },
    }
    write_json(output_dir / "web-admin-slsa-provenance.json", provenance)

    print(
        json.dumps(
            {
                "status": "passed",
                "artifact_sha256": artifact_digest,
                "node_base_digest": args.node_base_digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
