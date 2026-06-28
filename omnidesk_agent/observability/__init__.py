from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from omnidesk_agent.observability.metrics import DEFAULT_METRICS_REGISTRY, MetricsRegistry


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


def public_runtime_status(version: str) -> dict[str, Any]:
    status: dict[str, Any] = {"ok": True, "version": version}
    build_sha = os.getenv("OMNIDESK_BUILD_SHA")
    artifact_sha256 = os.getenv("OMNIDESK_ARTIFACT_SHA256")
    image_digest = os.getenv("OMNIDESK_IMAGE_DIGEST")
    if build_sha:
        status["build_sha"] = build_sha
    if artifact_sha256:
        status["artifact_sha256"] = artifact_sha256
    if image_digest:
        status["image_digest"] = image_digest
    return status


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
    "omnidesk_approval_resume_failures_total",
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
    "omnidesk_self_upgrade_invalid_transition_total",
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
    "omnidesk_webhook_signature_failures_total",
    "omnidesk_resume_attempts_total",
    "omnidesk_resume_success_total",
    "omnidesk_outbound_duplicate_total",
    "omnidesk_outbound_ambiguous_send_total",
    "omnidesk_outbound_dead_letter_total",
    "omnidesk_sandbox_timeout_total",
    "omnidesk_backup_verify_failure_total",
    "omnidesk_trace_spans_total",
    "omnidesk_run_duration_seconds",
    "omnidesk_tool_duration_seconds",
    "omnidesk_approval_wait_seconds",
    "omnidesk_sandbox_duration_seconds",
    "omnidesk_job_queue_lag_seconds",
    "omnidesk_job_dead_letter_total",
    "omnidesk_permission_denied_total",
    "omnidesk_upgrade_blocked_total",
    "omnidesk_trace_span_failures_total",
    "omnidesk_http_requests_total",
    "omnidesk_http_errors_total",
    "omnidesk_http_request_duration_seconds",
    "omnidesk_trace_export_failures_total",
]


def initialize_runtime_metrics(metrics: MetricsRegistry) -> None:
    for name in DEFAULT_RUNTIME_METRICS:
        metrics.inc(name, 0)


__all__ = [
    "DEFAULT_METRICS_REGISTRY",
    "DEFAULT_RUNTIME_METRICS",
    "JsonEventLogger",
    "MetricsRegistry",
    "initialize_runtime_metrics",
    "new_request_id",
    "public_runtime_status",
    "redact_runtime_status",
]
