#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
}
CONFLICT_MARKERS = ("<<<<<<< ", "=======\n", ">>>>>>> ")
TEXT_SUFFIXES = {
    ".css",
    ".dockerfile",
    ".env",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass
class Finding:
    path: Path
    message: str


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _looks_text(path: Path, data: bytes) -> bool:
    if b"\0" in data:
        return False
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _check_text_file(root: Path, path: Path, data: bytes) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(path, "text file is not valid UTF-8")]

    relative = path.relative_to(root)
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        stripped = line.rstrip("\n")
        if stripped.rstrip(" \t") != stripped:
            findings.append(Finding(relative, f"trailing whitespace at line {lineno}"))
        if line.startswith(CONFLICT_MARKERS):
            findings.append(Finding(relative, f"unresolved conflict marker at line {lineno}"))

    if text and not text.endswith("\n"):
        findings.append(Finding(relative, "missing final newline"))

    if "\r\n" in text:
        findings.append(Finding(relative, "CRLF line endings are not allowed"))

    return findings


def _check_python_file(root: Path, path: Path, data: bytes) -> list[Finding]:
    findings = _check_text_file(root, path, data)
    try:
        ast.parse(data.decode("utf-8"), filename=str(path))
    except SyntaxError as exc:
        findings.append(
            Finding(path.relative_to(root), f"python syntax error: {exc.msg} at line {exc.lineno}")
        )
    return findings


def run(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    required_paths = [
        "omnidesk_agent",
        "omnidesk_agent/server.py",
        "omnidesk_agent/config.py",
        "pyproject.toml",
    ]
    for item in required_paths:
        if not (root / item).exists():
            findings.append(Finding(Path(item), "required local runtime path is missing"))

    for path in _iter_files(root):
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(Finding(path.relative_to(root), f"cannot read file: {exc}"))
            continue

        if not _looks_text(path, data):
            continue

        if path.suffix == ".py":
            findings.extend(_check_python_file(root, path, data))
        else:
            findings.extend(_check_text_file(root, path, data))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check repository-wide local usability, style consistency, and conflict markers."
    )
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    findings = run(root)
    if findings:
        for finding in findings:
            print(f"{finding.path}: {finding.message}")
        return 1

    print("global consistency check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
