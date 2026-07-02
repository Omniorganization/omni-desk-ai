#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

try:
    from scripts import check_external_ga_evidence
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    import check_external_ga_evidence  # type: ignore[no-redef]


SCHEMA_VERSION = "omnidesk-external-ga-evidence-import/v1"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _required_evidence_files() -> list[str]:
    files: list[str] = []
    for spec in check_external_ga_evidence.REQUIRED_EVIDENCE.values():
        files.extend(str(path) for path in spec["files"])
    return sorted(dict.fromkeys(files))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _declared_version_issues(raw_dir: Path, expected_version: str | None) -> list[str]:
    if not expected_version:
        return []
    issues: list[str] = []
    for rel in _required_evidence_files():
        path = raw_dir / rel
        if not path.exists():
            continue
        try:
            doc = _read_json(path)
        except Exception:
            continue
        declared = str(doc.get("version") or "").strip()
        if declared and declared != expected_version:
            issues.append(f"{rel}: version must be {expected_version}, got {declared}")
    return issues


def _copy_tree_contents(source: Path, dest: Path) -> list[str]:
    source = source.resolve()
    dest = dest.resolve()
    if source == dest:
        raise ValueError(
            "raw evidence dir and destination dir must be different when --copy is used"
        )
    if _is_within(dest, source):
        raise ValueError("destination dir must not be inside raw evidence dir")

    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        rel = item.relative_to(source)
        target = (dest / rel).resolve()
        if not _is_within(target, dest):
            raise ValueError(f"refusing to copy path outside destination: {rel}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file():
            raise ValueError(f"unsupported evidence path type: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(str(rel))
    return copied


def validate_raw_dir(
    root: Path, raw_dir: Path, *, expected_version: str | None = None
) -> dict[str, Any]:
    root = root.resolve()
    raw_dir = raw_dir.resolve()
    issues: list[str] = []
    if not raw_dir.exists() or not raw_dir.is_dir():
        issues.append(
            f"raw evidence dir does not exist or is not a directory: {raw_dir}"
        )
        validation = {
            "status": "blocked_missing_external_evidence",
            "blocker_count": len(check_external_ga_evidence.REQUIRED_EVIDENCE),
        }
    else:
        validation = check_external_ga_evidence.audit(root, raw_dir)
        if validation.get("status") != "passed":
            issues.append(
                "external GA evidence audit did not pass for raw evidence dir"
            )
        version_issues = _declared_version_issues(raw_dir, expected_version)
        issues.extend(version_issues)

    return {
        "schema": SCHEMA_VERSION,
        "status": "passed" if not issues else "failed",
        "raw_dir": str(raw_dir),
        "expected_version": expected_version or "",
        "issues": issues,
        "validation": validation,
        "policy": "Importer copies external GA evidence only after the full raw bundle passes the real evidence gate; it never creates placeholder evidence.",
    }


def import_evidence(
    root: Path,
    raw_dir: Path,
    dest_dir: Path,
    *,
    expected_version: str | None = None,
    copy: bool = False,
) -> dict[str, Any]:
    report = validate_raw_dir(root, raw_dir, expected_version=expected_version)
    report["dest_dir"] = str(dest_dir.resolve())
    report["copied"] = False
    report["copied_files"] = []
    if report["status"] == "passed" and copy:
        report["copied_files"] = _copy_tree_contents(raw_dir, dest_dir)
        report["copied"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and import a complete external GA evidence bundle."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--raw-dir",
        required=True,
        help="Directory containing a complete release/external-evidence-shaped bundle.",
    )
    parser.add_argument("--dest-dir", default="release/external-evidence")
    parser.add_argument("--expected-version", default="")
    parser.add_argument(
        "--copy", action="store_true", help="Copy the validated bundle into dest-dir."
    )
    parser.add_argument(
        "--write-report", default="release/external-ga-evidence-import-report.json"
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_absolute():
        raw_dir = root / raw_dir
    dest_dir = Path(args.dest_dir)
    if not dest_dir.is_absolute():
        dest_dir = root / dest_dir

    report = import_evidence(
        root,
        raw_dir,
        dest_dir,
        expected_version=args.expected_version or None,
        copy=args.copy,
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
