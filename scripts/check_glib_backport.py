"""Fail closed on any vendor delta beyond the reviewed upstream two-line fix."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

ARCHIVE_SHA256 = "233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5"
UPSTREAM_FIX = "b5a4071e439bef2b5eea76c3aa25e5ae84839e34"
SOURCE = "src/variant_iter.rs"
PATCH = (
    (b"let p: *mut libc::c_char = std::ptr::null_mut();", b"let mut p: *mut libc::c_char = std::ptr::null_mut();"),
    (b"                &p,", b"                &mut p,"),
)


def check(root: Path, metadata: dict | None = None) -> dict:
    native = root / "apps/desktop-tauri/src-tauri"
    vendor = native / "vendor/glib"
    if vendor.is_symlink():
        raise ValueError("glib vendor root is a symlink")
    archive = (native / "vendor/glib-0.18.5.crate").read_bytes()
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("glib published archive checksum mismatch")
    expected_files: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for member in package.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.parts[0] != "glib-0.18.5" or ".." in path.parts:
                raise ValueError(f"unexpected archive entry: {member.name}")
            relative = str(PurePosixPath(*path.parts[1:]))
            expected_files.add(relative)
            handle = package.extractfile(member)
            if handle is None:
                raise ValueError(f"missing archive content: {relative}")
            expected = handle.read()
            if relative == ".cargo_vcs_info.json":
                expected = expected.rstrip(b"\n") + b"\n"
            if relative == SOURCE:
                for before, after in PATCH:
                    if expected.count(before) != 1:
                        raise ValueError("upstream baseline changed; patch requires review")
                    expected = expected.replace(before, after, 1)
            actual = vendor / relative
            if actual.is_symlink() or actual.read_bytes() != expected:
                raise ValueError(f"unreviewed glib source delta: {relative}")
    paths = list(vendor.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("glib vendor tree contains a symlink")
    if {str(path.relative_to(vendor)) for path in paths if path.is_file()} != expected_files:
        raise ValueError("glib vendor inventory mismatch")
    manifest = (native / "Cargo.toml").read_text(encoding="utf-8")
    if '[patch.crates-io]\nglib = { path = "vendor/glib" }' not in manifest:
        raise ValueError("glib source patch is not bound to desktop Cargo graph")
    lock = (native / "Cargo.lock").read_text(encoding="utf-8")
    entries = [entry for entry in lock.split("[[package]]") if '\nname = "glib"\n' in entry]
    if len(entries) != 1 or 'version = "0.18.5"' not in entries[0] or "source =" in entries[0]:
        raise ValueError("registry or unexpected glib survives in locked graph")
    if metadata is not None:
        packages = [p for p in metadata["packages"] if p["name"] == "glib"]
        if len(packages) != 1 or packages[0]["source"] is not None:
            raise ValueError("Cargo resolved a non-vendored or duplicate glib")
        if Path(packages[0]["manifest_path"]).resolve() != (vendor / "Cargo.toml").resolve():
            raise ValueError("Cargo resolved a different glib path")
        if packages[0]["id"] not in {node["id"] for node in metadata["resolve"]["nodes"]}:
            raise ValueError("patched glib is not in the resolved dependency graph")
    return {
        "archive_sha256": ARCHIVE_SHA256,
        "upstream_fix": UPSTREAM_FIX,
        "file_count": len(expected_files),
        "exact_patch_verified": True,
        "cargo_metadata_verified": metadata is not None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text()) if args.metadata else None
    report = check(args.root, metadata)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
