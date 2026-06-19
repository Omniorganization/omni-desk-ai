#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_KINDS = {
    "core_release": "core-release",
    "web_admin": "web-admin",
    "desktop": "desktop",
    "mobile": "mobile",
    "full_source": "full-source",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_version(version: str) -> str:
    return version.split("+", 1)[0]


def _default_slug(version: str) -> str:
    native = _native_version(version)
    suffix = version.split("+", 1)[1] if "+" in version else "source"
    return f"Omni-desk-AI-{native}-{suffix.replace('+', '-').replace('_', '-')}"


def _artifact_specs(version: str, package_slug: str) -> dict[str, str]:
    native = _native_version(version)
    return {
        "core_release": f"Omni-desk-AI-{native}-core-release.zip",
        "web_admin": f"Omni-desk-AI-{native}-web-admin.zip",
        "desktop": f"Omni-desk-AI-{native}-desktop.zip",
        "mobile": f"Omni-desk-AI-{native}-mobile.zip",
        "full_source": f"{package_slug}-full.zip",
    }


def _read_sha_manifest(package_dir: Path) -> dict[str, str]:
    path = package_dir / "SHA256SUMS.txt"
    checksums: dict[str, str] = {}
    if not path.exists():
        return checksums
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            digest, rel = raw.split(None, 1)
        except ValueError as exc:
            raise RuntimeError(f"SHA256SUMS.txt line {line_no} is malformed") from exc
        rel = rel.strip()
        if rel.startswith("/") or ".." in Path(rel).parts or "." in Path(rel).parts:
            raise RuntimeError(f"SHA256SUMS.txt line {line_no} is not portable: {rel}")
        checksums[rel] = digest
    return checksums


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _external_ga_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "status": "not_supplied",
            "blocker_count": None,
            "blocking_categories": [],
            "policy": "No external GA evidence audit was supplied for this distribution manifest.",
        }
    report = _load_json(path)
    categories = report.get("categories", {})
    blocking = [
        {
            "category": key,
            "label": value.get("label", key),
            "issues": value.get("issues", []),
        }
        for key, value in sorted(categories.items())
        if not value.get("ok")
    ]
    return {
        "status": report.get("status", "unknown"),
        "blocker_count": report.get("blocker_count", len(blocking)),
        "blocking_categories": blocking,
        "policy": report.get("policy", ""),
    }


def build_manifest(
    package_dir: Path,
    *,
    version: str,
    package_slug: str,
    source_commit: str,
    external_audit: Path | None,
) -> dict[str, Any]:
    checksums = _read_sha_manifest(package_dir)
    artifacts: list[dict[str, Any]] = []
    missing: list[str] = []
    for kind, filename in _artifact_specs(version, package_slug).items():
        path = package_dir / filename
        if not path.exists():
            missing.append(filename)
            continue
        actual = _sha256(path)
        recorded = checksums.get(filename)
        artifacts.append(
            {
                "kind": kind,
                "role": REQUIRED_KINDS[kind],
                "path": filename,
                "bytes": path.stat().st_size,
                "sha256": actual,
                "sha256_recorded": recorded,
                "sha256_recorded_matches": recorded == actual,
                "required": True,
            }
        )
    if missing:
        raise RuntimeError("missing required distribution artifacts: " + ", ".join(missing))

    external = _external_ga_summary(external_audit)
    blocker_count = external.get("blocker_count")
    status = "customer_distribution_ga" if blocker_count == 0 else "source_gated_production_ga_candidate"
    return {
        "schema_version": "omnidesk-distribution-manifest/v1",
        "version": version,
        "native_version": _native_version(version),
        "package_slug": package_slug,
        "release_status": status,
        "source_commit": source_commit,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "artifacts": artifacts,
        "external_ga_evidence": external,
        "policy": "This manifest verifies package integrity and source-gated release evidence. Customer-distribution GA still requires the external GA evidence gate to pass against real signed artifacts, live push delivery, staging soak, rollback, backup/restore, and self-healing drill evidence.",
    }


def write_manifest(package_dir: Path, manifest: dict[str, Any], output: Path) -> Path:
    target = output if output.is_absolute() else package_dir / output
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def verify_manifest(package_dir: Path, manifest_path: Path) -> list[str]:
    manifest = _load_json(manifest_path if manifest_path.is_absolute() else package_dir / manifest_path)
    issues: list[str] = []
    if manifest.get("schema_version") != "omnidesk-distribution-manifest/v1":
        issues.append("invalid or missing distribution manifest schema_version")
    seen_required = {item.get("kind") for item in manifest.get("artifacts", []) if item.get("required")}
    for kind in REQUIRED_KINDS:
        if kind not in seen_required:
            issues.append(f"missing required artifact kind in manifest: {kind}")
    checksums = _read_sha_manifest(package_dir)
    for item in manifest.get("artifacts", []):
        rel = item.get("path")
        if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
            issues.append(f"unsafe artifact path in manifest: {rel}")
            continue
        target = package_dir / rel
        if not target.is_file():
            issues.append(f"manifest artifact missing from package: {rel}")
            continue
        actual = _sha256(target)
        if item.get("sha256") != actual:
            issues.append(f"manifest sha256 mismatch for {rel}")
        recorded = checksums.get(rel)
        if recorded != actual:
            issues.append(f"SHA256SUMS mismatch or missing for {rel}")
    external = manifest.get("external_ga_evidence", {})
    blocker_count = external.get("blocker_count")
    if manifest.get("release_status") == "customer_distribution_ga" and blocker_count != 0:
        issues.append("manifest claims customer_distribution_ga while external GA blockers remain")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or verify an OmniDesk distribution package manifest.")
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--version")
    parser.add_argument("--package-slug")
    parser.add_argument("--source-commit", default="unknown")
    parser.add_argument("--external-audit")
    parser.add_argument("--output", default="release-manifest.json")
    parser.add_argument("--manifest", default="release-manifest.json")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)

    package_dir = Path(args.package_dir).resolve()
    try:
        if args.verify:
            issues = verify_manifest(package_dir, Path(args.manifest))
            if issues:
                for issue in issues:
                    print(issue, file=sys.stderr)
                return 1
            print("distribution package manifest ok")
            return 0
        if not args.version:
            print("--version is required when writing a distribution manifest", file=sys.stderr)
            return 2
        package_slug = args.package_slug or _default_slug(args.version)
        external = Path(args.external_audit).resolve() if args.external_audit else None
        manifest = build_manifest(
            package_dir,
            version=args.version,
            package_slug=package_slug,
            source_commit=args.source_commit,
            external_audit=external,
        )
        target = write_manifest(package_dir, manifest, Path(args.output))
        print(f"wrote distribution manifest: {target}")
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
