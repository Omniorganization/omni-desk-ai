#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
import json
import zipfile
from pathlib import Path


CHECKSUM_MANIFEST_NAME = "checksums.txt"
SIGNATURE_MANIFEST_NAME = "release_signatures.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_checksums(dist: Path) -> dict[str, str]:
    checksums = dist / CHECKSUM_MANIFEST_NAME
    if not checksums.exists():
        raise RuntimeError(f"{CHECKSUM_MANIFEST_NAME} is missing")
    result: dict[str, str] = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        result[name.strip()] = digest.strip()
    return result


def _version_from_wheel(path: Path) -> str | None:
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith(".dist-info/METADATA"):
                text = zf.read(name).decode("utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("Version: "):
                        return line.split(":", 1)[1].strip()
    return None


def _signable_artifact_names(dist: Path) -> set[str]:
    return {
        path.name
        for path in dist.iterdir()
        if path.is_file() and not path.name.endswith(".sig") and path.name != SIGNATURE_MANIFEST_NAME
    }


def _verify_signature_manifest_set(dist: Path, checksums: dict[str, str]) -> None:
    manifest_path = dist / SIGNATURE_MANIFEST_NAME
    if not manifest_path.exists():
        raise RuntimeError(f"{SIGNATURE_MANIFEST_NAME} is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise RuntimeError("signature manifest artifacts must be a list")
    signed = {str(item.get("name")) for item in artifacts}
    if len(signed) != len(artifacts):
        raise RuntimeError("signature manifest contains duplicate or missing artifact names")
    signable = _signable_artifact_names(dist)
    checksum_names = set(checksums)
    if signed != signable:
        raise RuntimeError(f"signature artifact set mismatch: signed={sorted(signed)} signable={sorted(signable)}")
    if CHECKSUM_MANIFEST_NAME not in signed:
        raise RuntimeError(f"{CHECKSUM_MANIFEST_NAME} must be signed")
    missing_sigs = sorted(name for name in signed if not (dist / f"{name}.sig").exists())
    if missing_sigs:
        raise RuntimeError(f"signature sidecar missing for artifacts: {missing_sigs}")
    checksum_required = signable - {CHECKSUM_MANIFEST_NAME}
    if checksum_names != checksum_required:
        raise RuntimeError(f"checksum artifact set mismatch: checksums={sorted(checksum_names)} required={sorted(checksum_required)}")
    for item in artifacts:
        name = str(item.get("name"))
        artifact = dist / name
        manifest_sha = str(item.get("sha256"))
        actual_sha = _sha256(artifact)
        if manifest_sha != actual_sha:
            raise RuntimeError(f"signature manifest sha256 mismatch for {name}")
        if name != CHECKSUM_MANIFEST_NAME and manifest_sha != checksums.get(name):
            raise RuntimeError(f"signature/checksum sha256 mismatch for {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an immutable release artifact directory before environment promotion.")
    parser.add_argument("dist_dir", nargs="?", default="dist")
    parser.add_argument("--expected-version", required=False)
    parser.add_argument("--require-signatures", action="store_true")
    parser.add_argument("--require-metadata", action="store_true", help="Require release_metadata.json and verify it matches the wheel.")
    parser.add_argument("--expected-artifact-sha256")
    parser.add_argument("--expected-image-digest")
    args = parser.parse_args(argv)

    dist = Path(args.dist_dir)
    if not dist.exists():
        print(f"dist directory missing: {dist}", file=sys.stderr)
        return 1
    try:
        expected = _read_checksums(dist)
        for name, digest in expected.items():
            artifact = dist / name
            if not artifact.exists():
                print(f"artifact listed in checksums.txt is missing: {name}", file=sys.stderr)
                return 1
            actual = _sha256(artifact)
            if actual != digest:
                print(f"checksum mismatch for {name}", file=sys.stderr)
                return 1
        wheels = sorted(dist.glob("*.whl"))
        if not wheels:
            print("no wheel artifact found", file=sys.stderr)
            return 1
        if args.expected_version:
            versions = {_version_from_wheel(wheel) for wheel in wheels}
            if versions != {args.expected_version}:
                print(f"wheel version mismatch: expected {args.expected_version}, got {sorted(versions)}", file=sys.stderr)
                return 1
        if args.require_signatures:
            _verify_signature_manifest_set(dist, expected)
        wheel = wheels[0]
        wheel_sha = _sha256(wheel)
        if args.expected_artifact_sha256 and wheel_sha != args.expected_artifact_sha256:
            print("wheel artifact sha256 mismatch", file=sys.stderr)
            return 1
        metadata_path = dist / "release_metadata.json"
        if args.require_metadata and not metadata_path.exists():
            print("release_metadata.json is required", file=sys.stderr)
            return 1
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("version") != _version_from_wheel(wheel):
                print("release metadata version mismatch", file=sys.stderr)
                return 1
            if metadata.get("artifact", {}).get("sha256") != wheel_sha:
                print("release metadata artifact sha256 mismatch", file=sys.stderr)
                return 1
            sbom_name = metadata.get("sbom", {}).get("name")
            sbom_sha = metadata.get("sbom", {}).get("sha256")
            if sbom_name:
                sbom_path = dist / str(sbom_name)
                if not sbom_path.exists():
                    print("release metadata sbom missing", file=sys.stderr)
                    return 1
                if _sha256(sbom_path) != sbom_sha:
                    print("release metadata sbom sha256 mismatch", file=sys.stderr)
                    return 1
            if args.expected_image_digest and metadata.get("image", {}).get("digest") != args.expected_image_digest:
                print("release metadata image digest mismatch", file=sys.stderr)
                return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("release artifact verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
