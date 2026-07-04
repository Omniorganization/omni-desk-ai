from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from omnidesk_agent.self_learning.schemas import LearningDraftArtifact, LearningFinding


class KnowledgeBuilder:
    """Build approval-gated knowledge, prompt and workflow drafts."""

    def build_drafts(self, findings: Iterable[LearningFinding]) -> list[LearningDraftArtifact]:
        drafts: list[LearningDraftArtifact] = []
        for finding in findings:
            if finding.finding_type == "knowledge_gap":
                drafts.append(self._knowledge_draft(finding))
            elif finding.finding_type == "prompt_issue":
                drafts.append(self._prompt_draft(finding))
            elif finding.finding_type in {"workflow_rule", "approval_policy", "tool_reliability", "evidence_gap"}:
                drafts.append(self._workflow_draft(finding))
        return drafts

    def write_pending_drafts(self, drafts: Iterable[LearningDraftArtifact], output_root: Path) -> list[Path]:
        paths: list[Path] = []
        root = output_root / "self_learning" / "pending_updates"
        for draft in drafts:
            target = root / draft.artifact_type / f"{draft.artifact_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._render_markdown(draft), encoding="utf-8")
            draft.metadata["pending_path"] = str(target)
            paths.append(target)
        return paths

    def _knowledge_draft(self, finding: LearningFinding) -> LearningDraftArtifact:
        body = "\n".join([
            "# Knowledge Entry Draft",
            "",
            f"Finding: {finding.title}",
            "",
            "Evidence:",
            json.dumps(finding.evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "",
            f"Recommended rule: {finding.recommended_action}",
            "",
            "Approval requirement: owner review before indexing into production RAG or memory.",
        ])
        return LearningDraftArtifact(
            artifact_type="knowledge",
            title=f"Knowledge update for {finding.title}",
            body=body,
            target="knowledge_base.pending",
            source_finding_id=finding.finding_id,
            metadata={"finding_type": finding.finding_type, "severity": finding.severity},
        )

    def _prompt_draft(self, finding: LearningFinding) -> LearningDraftArtifact:
        body = "\n".join([
            "# Prompt Template Draft",
            "",
            f"Finding: {finding.title}",
            "",
            "Prompt intent:",
            finding.recommended_action,
            "",
            "Guardrails:",
            "- Keep approval, audit and rollback requirements explicit.",
            "- Do not remove existing safety or production-evidence checks.",
            "- Treat this draft as pending until a human approves it.",
        ])
        return LearningDraftArtifact(
            artifact_type="prompt",
            title=f"Prompt update for {finding.title}",
            body=body,
            target="prompt_registry.pending",
            source_finding_id=finding.finding_id,
            metadata={"finding_type": finding.finding_type, "severity": finding.severity},
        )

    def _workflow_draft(self, finding: LearningFinding) -> LearningDraftArtifact:
        body = "\n".join([
            "# Workflow Rule Draft",
            "",
            f"Finding: {finding.title}",
            "",
            "Proposed rule:",
            finding.recommended_action,
            "",
            "Required evidence before promotion:",
            "- Regression or replay validation.",
            "- Human approval decision.",
            "- Rollback plan.",
        ])
        return LearningDraftArtifact(
            artifact_type="workflow",
            title=f"Workflow rule for {finding.title}",
            body=body,
            target="workflow_rules.pending",
            source_finding_id=finding.finding_id,
            metadata={"finding_type": finding.finding_type, "severity": finding.severity},
        )

    @staticmethod
    def _render_markdown(draft: LearningDraftArtifact) -> str:
        header = {
            "artifact_id": draft.artifact_id,
            "artifact_type": draft.artifact_type,
            "target": draft.target,
            "requires_approval": draft.requires_approval,
            "status": draft.status,
            "source_finding_id": draft.source_finding_id,
        }
        frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in header.items())
        return f"---\n{frontmatter}\n---\n\n{draft.body}\n"
