#!/usr/bin/env python3
from __future__ import annotations

import argparse
import filecmp
import json
import shutil
from pathlib import Path
from typing import Any

try:
    from scripts import import_external_ga_evidence
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    import import_external_ga_evidence  # type: ignore[no-redef]


SCHEMA_VERSION = "omnidesk-external-ga-evidence-assembly/v1"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _required_files() -> list[str]:
    return import_external_ga_evidence._required_evidence_files()  # noqa: SLF001


def _candidate_roots(source: Path, required_files: list[str]) -> list[Path]:
    candidates: list[Path] = []
    nested = source / "release" / "external-evidence"
    if nested.is_dir():
        candidates.append(nested)
    if any((source / rel).exists() for rel in required_files):
        candidates.append(source)
    return candidates


def _copy_candidate(candidate: Path, dest_dir: Path) -> tuple[list[str], list[str]]:
    copied: list[str] = []
    issues: list[str] = []
    source_root = candidate.resolve()
    dest_root = dest_dir.resolve()
    if source_root == dest_root or _is_within(dest_root, source_root):
        return copied, [f"destination must not be inside source candidate: {source_root}"]

    for item in sorted(source_root.rglob("*")):
        rel = item.relative_to(source_root)
        if any(part in {"", ".", ".."} for part in rel.parts):
            issues.append(f"unsupported evidence path: {rel}")
            continue
        target = (dest_root / rel).resolve()
        if not _is_within(target, dest_root):
            issues.append(f"refusing to copy path outside destination: {rel}")
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file():
            issues.append(f"unsupported evidence path type: {rel}")
            continue
        if target.exists() and not filecmp.cmp(item, target, shallow=False):
            issues.append(f"conflicting evidence file from multiple providers: {rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(str(rel))
    return copied, issues


def assemble_bundle(
    root: Path,
    source_dirs: list[Path],
    dest_dir: Path,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    dest_dir = dest_dir.resolve()
    required_files = _required_files()
    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "status": "failed",
        "dest_dir": str(dest_dir),
        "expected_version": expected_version or "",
        "sources": [],
        "copied_files": [],
        "issues": [],
        "validation": {},
        "policy": "The assembler only merges provider-produced evidence files and then runs the real external evidence gate. It never creates passing evidence.",
    }
    if not source_dirs:
        report["issues"].append("at least one provider or staging evidence source dir is required")

    dest_dir.mkdir(parents=True, exist_ok=True)
    for source in source_dirs:
        source = source.resolve()
        source_result: dict[str, Any] = {"source": str(source), "candidate_roots": [], "copied_files": [], "issues": []}
        if not source.exists() or not source.is_dir():
            source_result["issues"].append("source dir does not exist or is not a directory")
            report["sources"].append(source_result)
            continue
        candidates = _candidate_roots(source, required_files)
        source_result["candidate_roots"] = [str(candidate) for candidate in candidates]
        if not candidates:
            source_result["issues"].append(
                "source must contain release/external-evidence or required evidence paths"
            )
        for candidate in candidates:
            copied, issues = _copy_candidate(candidate, dest_dir)
            source_result["copied_files"].extend(copied)
            source_result["issues"].extend(issues)
            report["copied_files"].extend(copied)
        report["sources"].append(source_result)

    for source_result in report["sources"]:
        report["issues"].extend(
            f"{source_result['source']}: {issue}" for issue in source_result["issues"]
        )

    validation = import_external_ga_evidence.validate_raw_dir(
        root, dest_dir, expected_version=expected_version
    )
    report["validation"] = validation
    if validation.get("status") != "passed":
        report["issues"].append("assembled external GA evidence bundle did not pass validation")
    report["copied_files"] = sorted(dict.fromkeys(report["copied_files"]))
    report["status"] = "passed" if not report["issues"] else "failed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble BrowserStack/AWS/staging external GA evidence artifacts into one raw bundle."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--source-dir", action="append", default=[])
    parser.add_argument("--dest-dir", required=True)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--write-report", default="dist/external-ga-evidence-assembly.json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    dest_dir = Path(args.dest_dir)
    if not dest_dir.is_absolute():
        dest_dir = root / dest_dir
    source_dirs = []
    for value in args.source_dir:
        source = Path(value)
        if not source.is_absolute():
            source = root / source
        source_dirs.append(source)

    report = assemble_bundle(
        root,
        source_dirs,
        dest_dir,
        expected_version=args.expected_version or None,
    )
    report_path = Path(args.write_report)
    if not report_path.is_absolute():
        report_path = root / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
