from __future__ import annotations

import time
from typing import Any, Optional

from omnidesk_agent.self_learning.observability.audit import LearningAuditLog
from omnidesk_agent.self_learning.schemas import LearningSourceRecord


class LearningDataCollector:
    """Collect learning inputs without mutating runtime behavior."""

    def collect(
        self,
        *,
        memory: Any = None,
        audit_log: Optional[LearningAuditLog] = None,
        days: int = 7,
        limit: int = 200,
    ) -> list[LearningSourceRecord]:
        records: list[LearningSourceRecord] = []
        if memory is not None:
            records.extend(self.from_memory(memory, days=days, limit=limit))
        if audit_log is not None:
            records.extend(self.from_audit_log(audit_log, days=days, limit=limit))
        return records

    def from_memory(self, memory: Any, *, days: int = 7, limit: int = 200) -> list[LearningSourceRecord]:
        records: list[LearningSourceRecord] = []
        list_structured = getattr(memory, "list_structured", None)
        if callable(list_structured):
            for item in list_structured(days=days, limit=limit):
                occurred_at = float(item.get("created_at") or item.get("updated_at") or time.time())
                records.append(LearningSourceRecord(
                    source="memory.structured_experience",
                    payload=dict(item),
                    occurred_at=occurred_at,
                ))

        metrics_report = getattr(memory, "metrics_report", None)
        if callable(metrics_report):
            records.append(LearningSourceRecord(
                source="memory.learning_metrics",
                payload=metrics_report(days=days),
            ))

        summarize_failures = getattr(memory, "summarize_failures", None)
        if callable(summarize_failures):
            for item in summarize_failures(days=days, limit=min(limit, 50)):
                records.append(LearningSourceRecord(
                    source="memory.failure_summary",
                    payload=dict(item),
                ))
        return records

    def from_audit_log(self, audit_log: LearningAuditLog, *, days: int = 7, limit: int = 200) -> list[LearningSourceRecord]:
        records: list[LearningSourceRecord] = []
        for event in audit_log.read_days(days)[-limit:]:
            payload = event.to_dict()
            occurred_at = float(payload.get("created_at") or time.time())
            records.append(LearningSourceRecord(
                source=f"audit.{event.event_type}",
                payload=payload,
                occurred_at=occurred_at,
            ))
        return records
