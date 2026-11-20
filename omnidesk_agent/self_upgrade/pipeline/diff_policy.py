"""Check a model-written unified diff before it is applied anywhere.

The pipeline applies only plain text edits inside the repository, to files
outside the paths that hold policy, CI, release and deploy configuration, and
within size limits. AGENTS.md forbids AI changes that lower security policy,
widen token/secret/network access or weaken evidence gates; those live in the
restricted paths below, so a diff touching them is refused and needs a human.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from typing import Any

RESTRICTED_PATTERNS = (
    ".github/*", "scripts/*", "deploy/*", "infra/*", "release/*", "packaging/*",
    "omnidesk_agent/security/*", "omnidesk_agent/sandbox/*", "omnidesk_agent/plugins/*",
    "omnidesk_agent/self_upgrade/*", "omnidesk_agent/self_healing/*", "omnidesk_agent/api/*",
    "omnidesk_agent/server.py", "omnidesk_agent/server_routes/*", "omnidesk_agent/config.py",
    "AGENTS.md", "*/AGENTS.md", "CODEOWNERS", "*/CODEOWNERS", "SECURITY.md",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "requirements*.lock", "*.lock",
    "package.json", "package-lock.json", "Dockerfile", "*/Dockerfile", ".gitignore", ".gitattributes",
)
ALLOWED_SUFFIXES = (".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ts", ".tsx", ".dart", ".rs")
_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")


@dataclass
class DiffReport:
    ok: bool
    files: list[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiffPolicy:
    max_files: int = 10
    max_changed_lines: int = 400
    restricted_patterns: tuple[str, ...] = RESTRICTED_PATTERNS
    allowed_prefixes: tuple[str, ...] = ()  # empty = whole repo minus restricted

    def check(self, diff: str) -> DiffReport:
        report = DiffReport(ok=False)
        text = diff if diff.endswith("\n") else diff + "\n"
        if not text.strip():
            report.violations.append("empty diff")
            return report
        if "\x00" in text or "GIT binary patch" in text or "\nBinary files " in text:
            report.violations.append("binary content is not allowed")
        current: str | None = None
        for line in text.splitlines():
            header = _HEADER.match(line)
            if header:
                a, b = header.groups()
                current = b
                if a != b:
                    report.violations.append(f"renames/copies are not allowed: {a} -> {b}")
                self._check_path(b, report)
                if b not in report.files:
                    report.files.append(b)
                continue
            if line.startswith(("new file mode", "deleted file mode", "old mode", "new mode", "similarity index",
                                "rename from", "rename to", "copy from", "copy to")):
                mode = line.split()[-1]
                if line.startswith("deleted file mode"):
                    report.violations.append(f"file deletion is not allowed: {current}")
                elif "mode" in line and mode not in {"100644"}:
                    report.violations.append(f"file mode {mode} is not allowed: {current}")
                elif not line.startswith(("new file mode",)):
                    report.violations.append(f"{line.split()[0]} lines are not allowed: {current}")
                continue
            if line.startswith(("--- ", "+++ ")):
                path = line[4:].strip()
                if path != "/dev/null" and not path.startswith(("a/", "b/")):
                    report.violations.append(f"unexpected path header: {path}")
                continue
            if line.startswith("+") and not line.startswith("+++"):
                report.added_lines += 1
            elif line.startswith("-") and not line.startswith("---"):
                report.removed_lines += 1
        if not report.files:
            report.violations.append("no file headers (expected `diff --git a/... b/...`)")
        if len(report.files) > self.max_files:
            report.violations.append(f"{len(report.files)} files changed (max {self.max_files})")
        if report.added_lines + report.removed_lines > self.max_changed_lines:
            report.violations.append(f"{report.added_lines + report.removed_lines} changed lines (max {self.max_changed_lines})")
        report.ok = not report.violations
        return report

    def _check_path(self, path: str, report: DiffReport) -> None:
        if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            report.violations.append(f"path is not a plain relative path: {path}")
            return
        if any(fnmatch(path, pattern) for pattern in self.restricted_patterns):
            report.violations.append(f"restricted path needs a human change: {path}")
        if self.allowed_prefixes and not any(path.startswith(prefix) for prefix in self.allowed_prefixes):
            report.violations.append(f"outside the issue's scope: {path}")
        if not path.endswith(ALLOWED_SUFFIXES):
            report.violations.append(f"file type not allowed: {path}")
