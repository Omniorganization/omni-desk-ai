from __future__ import annotations


class SkillTestGenerator:
    def build_tests(self, candidate: dict) -> list[dict]:
        return [
            {
                "name": f"{candidate['name']}_has_trigger",
                "assertion": "skill spec includes task trigger and verification steps",
            },
            {
                "name": f"{candidate['name']}_requires_governance",
                "assertion": "skill remains candidate until replay and human review evidence exist",
            },
        ]
