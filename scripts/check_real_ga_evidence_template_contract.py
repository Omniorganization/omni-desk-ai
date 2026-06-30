#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REQUIRED_MANIFEST = Path("release/external-ga-evidence.required.json")
REAL_EVIDENCE_DIR = Path("release/external-evidence")


def _required_files(root: Path) -> set[str]:
    manifest = json.loads((root / REQUIRED_MANIFEST).read_text(encoding="utf-8"))
    files: set[str] = set()
    for value in (manifest.get("required_files") or {}).values():
        if isinstance(value, list):
            files.update(str(item) for item in value)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Real GA evidence template generator covers every required external evidence file.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    issues: list[str] = []

    with tempfile.TemporaryDirectory(prefix="omnidesk-evidence-templates-") as tmp:
        output_dir = Path(tmp) / "templates"
        cmd = [sys.executable, str(root / "scripts" / "write_external_ga_evidence_templates.py"), str(root), "--output-dir", str(output_dir)]
        result = subprocess.run(cmd, cwd=root, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            return result.returncode

        expected = _required_files(root)
        generated = {str(path.relative_to(output_dir)) for path in output_dir.rglob("*.json")}
        missing = sorted(expected - generated)
        extra = sorted(generated - expected)
        if missing:
            issues.append("missing generated templates: " + ", ".join(missing))
        if extra:
            issues.append("unexpected generated templates: " + ", ".join(extra))

        for rel in sorted(expected):
            path = output_dir / rel
            if not path.exists():
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                issues.append(f"invalid JSON template {rel}: {exc}")
                continue
            expected_path = f"release/external-evidence/{rel}"
            if doc.get("expected_evidence_file") != expected_path:
                issues.append(f"template {rel} does not point at {expected_path}")
            if str(doc.get("status") or "").strip().lower() in {"passed", "ok", "verified", "success"}:
                issues.append(f"template {rel} must not look like real passed evidence")

        real_dir = (root / REAL_EVIDENCE_DIR)
        if real_dir.exists() and any(real_dir.rglob("*.template.json")):
            issues.append("template files must not be stored under release/external-evidence")

        shutil.rmtree(output_dir, ignore_errors=True)

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print("real GA evidence template contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
