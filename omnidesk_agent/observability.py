from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


def new_request_id() -> str:
    return str(uuid.uuid4())


class JsonEventLogger:
    def __init__(self, name: str = "omnidesk"):
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def event(self, event: str, **fields: Any) -> None:
        payload = {"event": event, "ts": time.time(), **fields}
        self.logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


@dataclass
class MetricsRegistry:
    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.gauges[key] = value

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {"counters": dict(self.counters), "gauges": dict(self.gauges)}

    def counter_value(self, name: str, **labels: Any) -> float:
        key = self._key(name, labels)
        with self._lock:
            return float(self.counters.get(key, 0.0))

    def counter_sum(self, name: str, **required_labels: Any) -> float:
        prefix = name + "{"
        total = 0.0
        with self._lock:
            for key, value in self.counters.items():
                if key == name and not required_labels:
                    total += value
                    continue
                if not key.startswith(prefix):
                    continue
                if all(f'{label}="{str(val).replace(chr(34), chr(92)+chr(34))}"' in key for label, val in required_labels.items()):
                    total += value
        return total

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self.counters.items()):
                lines.append(f"{key} {value}")
            for key, value in sorted(self.gauges.items()):
                lines.append(f"{key} {value}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> str:
        if not labels:
            return name
        label_text = ",".join(f'{k}="{str(v).replace(chr(34), chr(92)+chr(34))}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_text}}}"


def public_runtime_status(version: str) -> dict[str, Any]:
    return {"ok": True, "version": version}


def redact_runtime_status(status: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(status)
    for key in ("workspace", "audit_log"):
        if key in redacted:
            redacted[key] = "<redacted>"
    return redacted


DEFAULT_RUNTIME_METRICS = [
    "agent_runs_total",
    "agent_runs_failed_total",
    "tool_calls_total",
    "tool_call_latency_seconds",
    "approval_required_total",
    "approval_denied_total",
    "resume_failed_total",
    "planner_fallback_total",
    "webhook_blocked_total",
    "plugin_failures_total",
    "self_upgrade_proposals_total",
    "memory_write_blocked_total",
    "bad_memory_detected_total",
    "omnidesk_planner_requests_total",
    "omnidesk_planner_results_total",
    "omnidesk_tool_calls_total",
    "omnidesk_approval_proposals_total",
    "omnidesk_approval_required_total",
    "omnidesk_approval_decisions_total",
    "omnidesk_approval_resume_grants_total",
    "omnidesk_approval_waiting_runs_total",
    "omnidesk_jobs_enqueued_total",
    "omnidesk_jobs_completed_total",
    "omnidesk_jobs_failed_total",
    "omnidesk_jobs_dead_lettered_total",
    "omnidesk_jobs_dead_letter_requeued_total",
    "omnidesk_jobs_dead_letter_purged_total",
    "omnidesk_outbound_messages_total",
    "omnidesk_jobs_stale_recovered_total",
    "omnidesk_plugin_load_total",
    "omnidesk_plugin_call_total",
    "omnidesk_self_upgrade_proposals_total",
    "omnidesk_self_upgrade_artifacts_total",
    "omnidesk_learning_experiments_created_total",
    "omnidesk_learning_experiment_assignments_total",
    "omnidesk_learning_experiment_observations_total",
    "omnidesk_learning_experiment_promotions_total",
    "omnidesk_memory_review_agent_votes_total",
    "omnidesk_skill_versions_registered_total",
    "omnidesk_skill_version_benchmarks_total",
    "omnidesk_causal_root_cause_reports_total",
    "omnidesk_learning_roi_evaluations_total",
    "omnidesk_world_model_observations_total",
    "omnidesk_webhook_enqueue_attempts_total",
    "omnidesk_webhook_enqueue_failures_total",
    "omnidesk_resume_attempts_total",
    "omnidesk_resume_success_total",
    "omnidesk_outbound_duplicate_total",
]



def initialize_runtime_metrics(metrics: MetricsRegistry) -> None:
    for name in DEFAULT_RUNTIME_METRICS:
        metrics.inc(name, 0)
