from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RecoveryAction = Literal[
    "none",
    "retry_with_backoff",
    "open_circuit_breaker",
    "switch_model_profile",
    "disable_capability",
    "rollback_release",
    "create_upgrade_proposal",
    "escalate_human_approval",
    "restart_worker",
    "recover_stale_jobs",
    "requeue_dead_letters",
    "rollback_config",
]


@dataclass(frozen=True)
class RuntimeSignal:
    component: str
    error_rate: float = 0.0
    consecutive_failures: int = 0
    safety_violation: bool = False
    health_ok: bool = True
    rollback_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    component: str
    reason: str
    autonomous: bool
    requires_human_approval: bool
    cooldown_seconds: int = 0
    rollback_ref: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeSelfHealingController:
    """Fail-closed self-healing policy for production runtime problems.

    The controller deliberately separates *runtime recovery* from *code mutation*:
    retries, circuit breakers, model fallback, and capability disablement may run
    autonomously; code changes, release rollback, and policy promotions require a
    signed release artifact or human-approved upgrade workflow.
    """

    def __init__(self, *, failure_threshold: int = 3, high_error_rate: float = 0.20):
        self.failure_threshold = max(1, int(failure_threshold))
        self.high_error_rate = float(high_error_rate)

    def decide(self, signal: RuntimeSignal) -> RecoveryDecision:
        component = signal.component.strip() or "unknown"
        meta = signal.metadata or {}
        if signal.safety_violation:
            return RecoveryDecision(
                action="disable_capability",
                component=component,
                reason="safety violation detected; fail closed before further automation",
                autonomous=True,
                requires_human_approval=True,
                cooldown_seconds=3600,
            )
        # A config snapshot is a rollback target too, but a cheaper and autonomous one.
        if component == "config" and not signal.health_ok and signal.rollback_ref:
            return RecoveryDecision(
                action="rollback_config",
                component=component,
                reason="configuration failed validation or health; restore the last known-good snapshot",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=900,
                rollback_ref=signal.rollback_ref,
            )
        if not signal.health_ok and signal.rollback_ref:
            return RecoveryDecision(
                action="rollback_release",
                component=component,
                reason="health check failed and a verified rollback artifact is available",
                autonomous=False,
                requires_human_approval=True,
                rollback_ref=signal.rollback_ref,
            )
        if component.startswith("worker:") and (not signal.health_ok or meta.get("hung_seconds")):
            return RecoveryDecision(
                action="restart_worker",
                component=component,
                reason="worker is not running" if not signal.health_ok else f"worker made no progress for {meta.get('hung_seconds')}s",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=60,
            )
        if component == "queue:stale_jobs" and int(meta.get("stale_count") or 0) > 0:
            return RecoveryDecision(
                action="recover_stale_jobs",
                component=component,
                reason="running jobs outlived their lease; return them to retry or dead-letter",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=60,
            )
        if component == "queue:dead_letters" and int(meta.get("transient_count") or 0) > 0:
            if not meta.get("dependency_healthy"):
                return RecoveryDecision(
                    action="none",
                    component=component,
                    reason="dead-lettered jobs failed transiently but their dependency is still unhealthy; wait",
                    autonomous=True,
                    requires_human_approval=False,
                )
            return RecoveryDecision(
                action="requeue_dead_letters",
                component=component,
                reason="jobs dead-lettered by transient errors; their dependency is healthy again",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=300,
            )
        if component.startswith("model") and signal.consecutive_failures >= self.failure_threshold:
            return RecoveryDecision(
                action="switch_model_profile",
                component=component,
                reason="model circuit breaker threshold reached; route to configured fallback profile",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=300,
            )
        if component.startswith("plugin") and signal.consecutive_failures >= self.failure_threshold:
            return RecoveryDecision(
                action="open_circuit_breaker",
                component=component,
                reason="plugin repeatedly failed; isolate it and prevent cascading failure",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=600,
            )
        if component.startswith("sandbox") and signal.error_rate >= self.high_error_rate:
            return RecoveryDecision(
                action="disable_capability",
                component=component,
                reason="sandbox error rate exceeded production threshold; disable remote execution pending review",
                autonomous=True,
                requires_human_approval=True,
                cooldown_seconds=1800,
            )
        if signal.consecutive_failures >= self.failure_threshold:
            return RecoveryDecision(
                action="create_upgrade_proposal",
                component=component,
                reason="repeated failure should become a governed self-upgrade proposal with tests and rollback plan",
                autonomous=False,
                requires_human_approval=True,
            )
        if signal.consecutive_failures > 0:
            return RecoveryDecision(
                action="retry_with_backoff",
                component=component,
                reason="transient failure below threshold; retry with bounded backoff",
                autonomous=True,
                requires_human_approval=False,
                cooldown_seconds=30 * signal.consecutive_failures,
            )
        return RecoveryDecision(
            action="none",
            component=component,
            reason="runtime signal is healthy",
            autonomous=True,
            requires_human_approval=False,
        )
