from __future__ import annotations

from pathlib import Path
import re


class SkillSandbox:
    """Create low-risk skills without changing core runtime files."""

    SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")

    def __init__(self, skills_root: Path):
        self.skills_root = skills_root.expanduser()
        self.skills_root.mkdir(parents=True, exist_ok=True)

    def create_skill(self, name: str, markdown: str, *, overwrite: bool = False) -> Path:
        if not self.SAFE_NAME.match(name):
            raise ValueError("skill name may only contain letters, numbers, dots, dashes and underscores")
        path = self.skills_root / name / "SKILL.md"
        if path.exists() and not overwrite:
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path
