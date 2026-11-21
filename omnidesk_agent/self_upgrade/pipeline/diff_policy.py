"""Check a model-written unified diff before it is applied anywhere.

The pipeline applies only plain text edits, within size limits, to files
inside the issue's ``scope:`` (a subdirectory or file of ``omnidesk_agent/``
or ``docs/``) plus ``tests/``, and never to the paths below. AGENTS.md forbids
AI changes that lower security policy, widen token/secret/network access or
weaken evidence gates. Those live in the restricted paths (CI, scripts,
release and deploy files, security, approval, auth, signature, sandbox and
autonomy code, dependency manifests at any depth), so a diff touching them is
refused and needs a human. ``fnmatch``'s ``*`` also matches ``/``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from typing import Any

RESTRICTED_PATTERNS = (
    # CI, scripts, release, deploy and packaging
    ".github/*", "scripts/*", "deploy/*", "infra/*", "release/*", "packaging/*",
    # security, approval, auth, signature, sandbox, plugin, API and autonomy code
    "omnidesk_agent/security/*", "omnidesk_agent/sandbox/*", "omnidesk_agent/plugins/*", "omnidesk_agent/oauth/*",
    "omnidesk_agent/validation/*", "omnidesk_agent/self_upgrade/*", "omnidesk_agent/self_healing/*",
    "omnidesk_agent/self_learning/*", "omnidesk_agent/api/*", "omnidesk_agent/server.py", "omnidesk_agent/server_routes/*",
    "omnidesk_agent/config.py",
    "*approval*", "*approve*", "*auth*", "*signature*", "*signing*", "*permission*", "*rbac*", "*policy*",
    "*secret*", "*credential*", "*csp*",
    # repository rules
    "AGENTS.md", "*/AGENTS.md", "CODEOWNERS", "*/CODEOWNERS", "SECURITY.md", ".gitignore", "*/.gitignore", ".gitattributes",
    # dependency manifests and lock files, at any depth
    "pyproject.toml", "*/pyproject.toml", "setup.py", "*/setup.py", "setup.cfg", "*/setup.cfg",
    "requirements*", "*/requirements*", "*.lock", "*-lock.json", "*-lock.yaml", "*.lockb",
    "package.json", "*/package.json", "Cargo.toml", "*/Cargo.toml", "pubspec.yaml", "*/pubspec.yaml",
    "go.mod", "*/go.mod", "go.sum", "*/go.sum", "Gemfile*", "*/Gemfile*", "Podfile*", "*/Podfile*", "*.gradle", "*.gradle.kts",
    "Dockerfile*", "*/Dockerfile*", "*.dockerfile",
)
# Where an issue's scope may point. Everything else (apps/, packages/, ...) holds
# configuration such as the desktop CSP, signing and app permissions.
ALLOWED_SCOPE_ROOTS = ("omnidesk_agent/", "docs/")
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
    allowed_prefixes: tuple[str, ...] = ()  # empty allows nothing: every change needs a scope

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
                if path == "/dev/null":
                    if line.startswith("+++ "):
                        report.violations.append(f"file deletion is not allowed: {current}")
                    continue
                prefix = "a/" if line.startswith("--- ") else "b/"
                # git apply may use these names; they must be the file the git header names.
                if not path.startswith(prefix) or current is None or path[2:] != current:
                    report.violations.append(f"path header {path!r} does not match diff --git header {current!r}")
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
        if not any(_within(path, prefix) for prefix in self.allowed_prefixes):
            report.violations.append(f"outside the issue's scope: {path}" if self.allowed_prefixes else f"no scope allows this change: {path}")
        if not path.endswith(ALLOWED_SUFFIXES):
            report.violations.append(f"file type not allowed: {path}")


def _within(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def scoped_policy(issue: Any) -> DiffPolicy:
    """The one policy all three steps check: the issue's ``scope:`` paths plus ``tests/``.

    An issue must name its scope, inside a subdirectory or file of
    ``omnidesk_agent/`` or ``docs/``. Without one the model could write anywhere
    the restricted list does not cover.
    """
    scope = tuple(str(path) for path in (getattr(issue, "scope_paths", None) or ()))
    if not scope:
        raise ValueError("the issue names no scope: add a `scope:` line with the paths the change may touch")
    for path in scope:
        if not path.startswith(ALLOWED_SCOPE_ROOTS) or path.rstrip("/") + "/" in ALLOWED_SCOPE_ROOTS:
            raise ValueError(f"scope {path!r} must be a subdirectory or file of {' or '.join(ALLOWED_SCOPE_ROOTS)}")
    return DiffPolicy(allowed_prefixes=scope + ("tests/",))
