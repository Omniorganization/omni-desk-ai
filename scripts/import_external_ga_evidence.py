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

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EVIDENCE_CONTRACT_PATH = ROOT / "release" / "external-ga-evidence.required.json"
# Written by Main Verification next to the provider evidence and read by the
# final Real GA gates.
CONTROL_PLANE_EVIDENCE_FILES = (
    "control-plane/main-verification-evidence.json",
    "control-plane/native-signed-artifact-binding.json",
    "control-plane/github-branch-protection-live.json",
    "control-plane/github-team-governance-live.json",
)
# Written by registered producer workflows (scripts/browserstack_android_smoke.py)
# and kept for the audit trail although no gate requires them.
PRODUCER_EVIDENCE_FILES = ("mobile-real-device/browserstack-android-smoke.json",)
# Evidence is data. A referenced artifact with one of these suffixes (or a
# conftest.py) would be importable code once it lands in the workspace.
CODE_FILE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyd", ".pth", ".so")


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


def expected_evidence_json_files() -> list[str]:
    """Every evidence JSON path a consumer may take from an evidence bundle."""
    files = set(_required_evidence_files())
    policy = json.loads(check_external_ga_evidence.EVIDENCE_POLICY_PATH.read_text(encoding="utf-8"))
    for spec in (policy.get("extended_categories") or {}).values():
        files.update(str(path) for path in spec.get("files", []))
    contract = json.loads(REQUIRED_EVIDENCE_CONTRACT_PATH.read_text(encoding="utf-8"))
    for paths in (contract.get("required_files") or {}).values():
        files.update(str(path) for path in paths)
    files.update(CONTROL_PLANE_EVIDENCE_FILES)
    files.update(PRODUCER_EVIDENCE_FILES)
    return sorted(files)


def _referenced_artifact_paths(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return []
    refs: list[str] = []
    artifacts = doc.get("artifacts")
    if isinstance(artifacts, list):
        refs.extend(str(item["path"]) for item in artifacts if isinstance(item, dict) and isinstance(item.get("path"), str))
    if isinstance(doc.get("signed_artifact"), str):
        refs.append(doc["signed_artifact"])
    return refs


def _is_code_file(rel: Path) -> bool:
    return rel.name == "conftest.py" or rel.name.lower().endswith(CODE_FILE_SUFFIXES)


def select_expected_evidence_files(source: Path) -> dict[str, list[str]]:
    """Picks the expected evidence JSON files and the artifacts they reference.

    Everything else in the bundle (scripts, conftest.py, unexpected JSON) is
    reported as ignored and never copied. Referenced artifacts must be relative
    regular files inside the bundle and must not be code.
    """
    source = source.resolve()
    selected: list[str] = []
    issues: list[str] = []

    def take(rel_text: str, *, reference: bool) -> str | None:
        rel = Path(rel_text)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            if reference:
                return None  # resolved against the repository root by the evidence gate
            issues.append(f"unsupported evidence path: {rel_text}")
            return None
        path = source / rel
        if path.is_symlink():
            issues.append(f"refusing to copy symlinked evidence path: {rel_text}")
            return None
        if not path.is_file():
            return None
        if not _is_within(path.resolve(), source):
            issues.append(f"refusing to copy path outside the evidence bundle: {rel_text}")
            return None
        if _is_code_file(rel):
            issues.append(f"refusing to copy code file referenced as evidence: {rel_text}")
            return None
        if rel.as_posix() not in selected:
            selected.append(rel.as_posix())
        return rel.as_posix()

    for rel_text in expected_evidence_json_files():
        if take(rel_text, reference=False) is None:
            continue
        try:
            doc = json.loads((source / rel_text).read_text(encoding="utf-8"))
        except Exception:
            continue  # the evidence gate reports invalid JSON
        for ref in _referenced_artifact_paths(doc):
            take(ref, reference=True)

    present = sorted(
        item.relative_to(source).as_posix()
        for item in source.rglob("*")
        if item.is_file() or item.is_symlink()
    )
    return {
        "selected": sorted(selected),
        "ignored": [rel for rel in present if rel not in selected],
        "issues": issues,
    }


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


def copy_expected_evidence(source: Path, dest: Path) -> dict[str, list[str]]:
    """Copies only the expected evidence files (see select_expected_evidence_files)."""
    source = source.resolve()
    dest = dest.resolve()
    if source == dest:
        raise ValueError(
            "raw evidence dir and destination dir must be different when --copy is used"
        )
    if _is_within(dest, source):
        raise ValueError("destination dir must not be inside raw evidence dir")

    selection = select_expected_evidence_files(source)
    if selection["issues"]:
        raise ValueError("; ".join(selection["issues"]))
    dest.mkdir(parents=True, exist_ok=True)
    for rel in selection["selected"]:
        target = (dest / rel).resolve()
        if not _is_within(target, dest):
            raise ValueError(f"refusing to copy path outside destination: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    return {"copied": selection["selected"], "ignored": selection["ignored"]}


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
        "policy": "Importer copies only the expected evidence JSON files and the artifacts they reference, and only after the full raw bundle passes the real evidence gate; it never creates placeholder evidence.",
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
    report["ignored_files"] = []
    if report["status"] == "passed" and copy:
        try:
            result = copy_expected_evidence(raw_dir, dest_dir)
        except ValueError as exc:
            report["issues"].append(str(exc))
            report["status"] = "failed"
            return report
        report["copied_files"] = result["copied"]
        report["ignored_files"] = result["ignored"]
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
