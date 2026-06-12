from __future__ import annotations

import json
from pathlib import Path

from omnidesk_agent.self_learning.skill_learning.spec_builder import SkillSpecBuilder
from omnidesk_agent.self_learning.skill_learning.test_generator import SkillTestGenerator


class SkillRegistryBridge:
    def __init__(self, root: Path):
        self.root = root.expanduser()
        self.spec_builder = SkillSpecBuilder()
        self.test_generator = SkillTestGenerator()

    def write_candidate(self, candidate: dict) -> dict:
        path = self.root / candidate["name"]
        path.mkdir(parents=True, exist_ok=True)
        skill_path = path / "SKILL.md"
        tests_path = path / "skill_tests.json"
        skill_path.write_text(self.spec_builder.build_markdown(candidate), encoding="utf-8")
        tests_path.write_text(json.dumps(self.test_generator.build_tests(candidate), ensure_ascii=False, indent=2), encoding="utf-8")
        return {**candidate, "skill_path": str(skill_path), "tests_path": str(tests_path)}
