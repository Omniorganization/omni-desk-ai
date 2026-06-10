from __future__ import annotations

import re
from pathlib import Path


class UpgradeSecurityChecker:
    """Static guardrails for generated patches."""

    BLOCK_PATTERNS = [
        r"auto_merge\s*=\s*True",
        r"force_push",
        r"reset\s+--hard\s+origin/main",
        r"approval_mode\s*=\s*['\"]allow['\"]",
        r"always_ask_tools\s*=\s*\[\]",
        r"rm\s+-rf\s+/",
        r"document\.cookie",
        r"localStorage",
        r"eval\(",
    ]

    def check_files(self, repo_root: Path, files: list[str]) -> dict:
        issues = []
        for rel in files:
            path = (repo_root / rel).resolve()
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pat in self.BLOCK_PATTERNS:
                if re.search(pat, text):
                    issues.append({"file": rel, "pattern": pat})
        return {"ok": not issues, "issues": issues}
