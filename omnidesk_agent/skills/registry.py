from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

@dataclass(slots=True)
class Skill:
    name: str
    path: Path
    instructions: str
    def score(self, query: str) -> int:
        q = query.lower()
        score = 10 if self.name.lower() in q else 0
        words = set(re.findall(r"[a-zA-Z0-9_\-\u4e00-\u9fff]+", q))
        haystack = (self.name + "\n" + self.instructions[:2000]).lower()
        for w in words:
            if len(w) >= 2 and w in haystack:
                score += 1
        return score

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
    def match(self, query: str, limit: int = 5) -> list[Skill]:
        scored = [(s.score(query), s) for s in self.skills.values()]
        return [s for score, s in sorted(scored, key=lambda x: x[0], reverse=True) if score > 0][:limit]
    def prompt_block(self, query: str | None = None, max_chars: int = 6000) -> str:
        skills = self.match(query, limit=5) if query else list(self.skills.values())
        if not skills:
            return ""
        parts = ["# Loaded Skills Used By Planner"]
        remaining = max_chars
        for s in skills:
            if remaining <= 0:
                break
            content = s.instructions[: min(4000, remaining)]
            chunk = f"\n## {s.name}\n{content}"
            parts.append(chunk)
            remaining -= len(chunk)
        return "\n".join(parts)
    def validate(self) -> dict:
        return {"skill_dirs": [str(d.expanduser()) for d in self.skill_dirs], "loaded_count": len(self.skills), "skills": sorted(self.skills.keys())}
