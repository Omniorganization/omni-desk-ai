"""Runtime self-healing: detect from health signals, decide, actuate, verify.

``policy`` is the decision table. ``probes`` turn runtime state into
``RuntimeSignal``s, ``actuators`` carry out decisions, and ``supervisor`` runs
the loop with cooldowns, budgets, an observe-only mode and an audit log.
``watchdog`` is the out-of-process loop for failures that stop the gateway
itself (crash loop); it restarts the gateway and reports a changed config,
but never reverts one.
"""
from __future__ import annotations

from omnidesk_agent.self_healing.policy import (
    RecoveryAction,
    RecoveryDecision,
    RuntimeSelfHealingController,
    RuntimeSignal,
)

__all__ = ["RecoveryAction", "RecoveryDecision", "RuntimeSelfHealingController", "RuntimeSignal"]
