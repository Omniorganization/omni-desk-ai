from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Skill:
    name: str
    path: Path
    instructions: str


class SkillRegistry:
    def __init__(self, skill_dirs: list[Path]):
        self.skill_dirs = skill_dirs
        self.skills: dict[str, Skill] = {}

    def load(self) -> dict[str, Skill]:
        self.skills.clear()
        for root in self.skill_dirs:
            root = root.expanduser()
            if not root.exists():
                continue
            for p in root.glob("*/SKILL.md"):
                name = p.parent.name
                self.skills[name] = Skill(name=name, path=p, instructions=p.read_text(encoding="utf-8"))
        return self.skills

    def prompt_block(self) -> str:
        if not self.skills:
            return ""
        parts = ["# Loaded Skills"]
        for s in self.skills.values():
            parts.append(f"\n## {s.name}\n{s.instructions[:4000]}")
        return "\n".join(parts)
