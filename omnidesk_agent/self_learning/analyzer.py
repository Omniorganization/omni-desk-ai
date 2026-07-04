from __future__ import annotations

from collections import Counter, defaultdict

from omnidesk_agent.self_learning.schemas import LearningFinding, LearningSourceRecord


class LearningAnalyzer:
    """Turn collected records into reviewable findings."""

    def analyze(self, records: list[LearningSourceRecord]) -> list[LearningFinding]:
        findings: list[LearningFinding] = []
        findings.extend(self._failure_clusters(records))
        findings.extend(self._metric_findings(records))
        findings.extend(self._approval_findings(records))
        findings.extend(self._evidence_gap_findings(records))
        return self._dedupe(findings)

    def _failure_clusters(self, records: list[LearningSourceRecord]) -> list[LearningFinding]:
        grouped: dict[str, list[LearningSourceRecord]] = defaultdict(list)
        for record in records:
            payload = record.payload
            reason = str(payload.get("failure_reason") or payload.get("reason") or "").strip()
            if not reason:
                continue
            if payload.get("success") is False or record.source.endswith("failure_summary"):
                grouped[reason].append(record)

        findings: list[LearningFinding] = []
        for reason, items in grouped.items():
            count = sum(int(item.payload.get("count", 1) or 1) for item in items)
            severity = "high" if count >= 5 else "medium" if count >= 2 else "low"
            finding_type = self._classify_failure_type(reason)
            findings.append(LearningFinding(
                finding_type=finding_type,
                title=f"Repeated failure: {reason[:80]}",
                severity=severity,
                evidence={"failure_reason": reason, "count": count},
                recommended_action=self._recommended_action(finding_type, reason),
                source_record_ids=[item.record_id for item in items],
            ))
        return findings

    def _metric_findings(self, records: list[LearningSourceRecord]) -> list[LearningFinding]:
        findings: list[LearningFinding] = []
        for record in records:
            if record.source != "memory.learning_metrics":
                continue
            payload = record.payload
            if float(payload.get("tool_error_rate", 0.0) or 0.0) >= 0.15:
                findings.append(LearningFinding(
                    finding_type="tool_reliability",
                    title="Tool error rate exceeds learning threshold",
                    severity="high",
                    evidence={"tool_error_rate": payload.get("tool_error_rate"), "totals": payload.get("totals")},
                    recommended_action="Add fallback, retry and targeted regression coverage for the failing tool chain.",
                    source_record_ids=[record.record_id],
                ))
            if float(payload.get("manual_intervention_rate", 0.0) or 0.0) >= 0.20:
                findings.append(LearningFinding(
                    finding_type="workflow_rule",
                    title="Manual intervention rate exceeds learning threshold",
                    severity="medium",
                    evidence={"manual_intervention_rate": payload.get("manual_intervention_rate"), "totals": payload.get("totals")},
                    recommended_action="Generate a workflow rule proposal that adds an explicit approval or fallback step.",
                    source_record_ids=[record.record_id],
                ))
        return findings

    def _approval_findings(self, records: list[LearningSourceRecord]) -> list[LearningFinding]:
        rejected = [r for r in records if str(r.payload.get("outcome") or r.payload.get("decision") or "").lower() in {"rejected", "denied"}]
        if not rejected:
            return []
        counter = Counter(str(r.payload.get("approval_type") or r.payload.get("event_type") or "approval") for r in rejected)
        return [
            LearningFinding(
                finding_type="approval_policy",
                title=f"Approval rejections need policy review: {kind}",
                severity="medium",
                evidence={"approval_type": kind, "rejection_count": count},
                recommended_action="Draft a workflow rule that clarifies required evidence before this action is requested again.",
                source_record_ids=[r.record_id for r in rejected if str(r.payload.get("approval_type") or r.payload.get("event_type") or "approval") == kind],
            )
            for kind, count in counter.items()
        ]

    def _evidence_gap_findings(self, records: list[LearningSourceRecord]) -> list[LearningFinding]:
        findings: list[LearningFinding] = []
        for record in records:
            text = " ".join(str(v).lower() for v in record.payload.values())
            if "missing" in text and "evidence" in text:
                findings.append(LearningFinding(
                    finding_type="evidence_gap",
                    title="Evidence gap detected in learning input",
                    severity="high",
                    evidence={"source": record.source, "payload": record.payload},
                    recommended_action="Add a release or workflow gate that captures real evidence before promotion.",
                    source_record_ids=[record.record_id],
                ))
        return findings

    @staticmethod
    def _classify_failure_type(reason: str) -> str:
        lower = reason.lower()
        if any(word in lower for word in ("prompt", "answer", "response template", "system instruction")):
            return "prompt_issue"
        if any(word in lower for word in ("knowledge", "rag", "docs", "outdated")):
            return "knowledge_gap"
        if any(word in lower for word in ("selector", "api", "exception", "traceback", "bug")):
            return "code_gap"
        if any(word in lower for word in ("approval", "policy", "permission")):
            return "approval_policy"
        return "workflow_rule"

    @staticmethod
    def _recommended_action(finding_type: str, reason: str) -> str:
        if finding_type == "prompt_issue":
            return f"Draft a prompt template update that prevents repeated failures caused by: {reason}"
        if finding_type == "knowledge_gap":
            return f"Draft a reviewed knowledge entry for the stale or missing topic: {reason}"
        if finding_type == "code_gap":
            return f"Prepare a code-fix PR proposal with regression tests for: {reason}"
        if finding_type == "approval_policy":
            return f"Draft an approval workflow rule for: {reason}"
        return f"Draft a workflow improvement and test coverage for: {reason}"

    @staticmethod
    def _dedupe(findings: list[LearningFinding]) -> list[LearningFinding]:
        seen: set[tuple[str, str]] = set()
        unique: list[LearningFinding] = []
        for finding in findings:
            key = (finding.finding_type, finding.title)
            if key in seen:
                continue
            seen.add(key)
            unique.append(finding)
        return unique
