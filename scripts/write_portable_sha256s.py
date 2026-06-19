#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(base: Path, path: Path) -> str:
    rel = path.resolve().relative_to(base.resolve())
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"unsafe relative checksum path: {path}")
    return rel.as_posix()


def write_manifest(base: Path, output: Path, files: list[Path]) -> None:
    base = base.resolve()
    rows: list[str] = []
    for item in sorted(files, key=lambda value: value.name):
        path = item if item.is_absolute() else base / item
        if not path.is_file():
            raise FileNotFoundError(f"checksum input is not a file: {path}")
        rows.append(f"{_sha256(path)}  {_safe_relative(base, path)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")


def verify_manifest(base: Path, manifest: Path) -> list[str]:
    issues: list[str] = []
    for line_no, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            expected, rel = line.split(None, 1)
        except ValueError:
            issues.append(f"line {line_no}: expected '<sha256>  <relative-path>'")
            continue
        rel = rel.strip()
        if rel.startswith("/") or ".." in Path(rel).parts or "." in Path(rel).parts:
            issues.append(f"line {line_no}: checksum path must be portable and relative: {rel}")
            continue
        target = base / rel
        if not target.is_file():
            issues.append(f"line {line_no}: checksum target is missing: {rel}")
            continue
        actual = _sha256(target)
        if actual != expected:
            issues.append(f"line {line_no}: checksum mismatch for {rel}: expected {expected}, got {actual}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or verify portable SHA256SUMS manifests with relative paths.")
    parser.add_argument("--base-dir", default=".", help="Base directory for relative checksum paths.")
    parser.add_argument("--output", default="SHA256SUMS.txt", help="Manifest path to write.")
    parser.add_argument("--verify", action="store_true", help="Verify an existing manifest instead of writing one.")
    parser.add_argument("files", nargs="*", help="Files to include when writing the manifest.")
    args = parser.parse_args(argv)

    base = Path(args.base_dir)
    output = Path(args.output)
    if args.verify:
        manifest = output if output.is_absolute() else base / output
        issues = verify_manifest(base, manifest)
        if issues:
            for issue in issues:
                print(issue, file=sys.stderr)
            return 1
        print("portable sha256 manifest ok")
        return 0
    if not args.files:
        print("at least one file is required when writing a checksum manifest", file=sys.stderr)
        return 2
    manifest = output if output.is_absolute() else base / output
    write_manifest(base, manifest, [Path(item) for item in args.files])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
