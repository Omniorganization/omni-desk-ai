from __future__ import annotations


class SkillSpecBuilder:
    def build_markdown(self, candidate: dict) -> str:
        return (
            f"# {candidate['name']}\n\n"
            "## Purpose\n"
            f"Handle `{candidate.get('goal', '')}` using a validated learned workflow.\n\n"
            "## Trigger\n"
            f"Use when task_type is `{candidate.get('task_type', 'unknown')}` or the user goal resembles `{candidate.get('goal', '')}`.\n\n"
            "## Procedure\n"
            f"1. Confirm the goal and risk level.\n"
            f"2. Apply this recommended action: {candidate.get('recommended_action', '')}\n"
            "3. Verify the expected outcome before writing to long-term memory.\n"
            "4. Escalate to human review if confidence drops or the environment changed.\n\n"
            "## Governance\n"
            f"Source experience: {candidate.get('source_experience_id')}\n"
            f"Confidence: {candidate.get('confidence')}\n"
            "Status: candidate; requires tests before canary or stable promotion.\n"
        )
