"""The self-healing loop: probe -> decide -> act (within budget) -> verify -> audit.

Modes: ``off`` does nothing; ``observe`` decides and audits but never acts;
``act`` runs actuators. Every action is bounded by the decision's cooldown, a
per-component limit per hour and a global limit per hour. A component whose
actions keep failing verification is escalated and no longer acted on until
it recovers or an operator resets it (the healer's own circuit breaker).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional

from omnidesk_agent.self_healing.actuators import ActionResult, Actuator
from omnidesk_agent.self_healing.policy import RecoveryDecision, RuntimeSelfHealingController, RuntimeSignal

Mode = Literal["off", "observe", "act"]
# Decisions whose actuator is itself the request for a human decision.
HUMAN_ROUTED = frozenset({"create_upgrade_proposal", "escalate_human_approval", "rollback_release", "disable_capability"})


@dataclass(frozen=True)
class HealingBudget:
    max_actions_per_component_per_hour: int = 3
    max_actions_per_hour: int = 20
    failed_verifications_before_escalation: int = 3


class HealingSupervisor:
    def __init__(
        self,
        probes: Iterable[Any],
        actuators: Iterable[Actuator],
        *,
        audit_path: Path,
        mode: Mode = "observe",
        controller: Optional[RuntimeSelfHealingController] = None,
        budget: Optional[HealingBudget] = None,
        verify_delay_seconds: float = 1.0,
        action_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if mode not in ("off", "observe", "act"):
            raise ValueError("mode must be off, observe or act")
        self.probes = list(probes)
        self.actuators = list(actuators)
        self.audit_path = Path(audit_path).expanduser()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.mode: Mode = mode
        self.controller = controller or RuntimeSelfHealingController()
        self.budget = budget or HealingBudget()
        self.verify_delay_seconds = float(verify_delay_seconds)
        self.action_timeout_seconds = float(action_timeout_seconds)
        self.clock = clock
        self.sleep = sleep
        self._actions: dict[str, deque[float]] = defaultdict(deque)
        self._all_actions: deque[float] = deque()
        self._last_action: dict[str, float] = {}
        self._failed_verifications: dict[str, int] = defaultdict(int)
        self._quarantined: set[str] = set()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ loop
    async def run_forever(self, *, interval_seconds: float = 15.0) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # the healer must not crash the gateway
                self._audit({"event": "tick_error", "error_class": type(exc).__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def reset(self, component: str) -> None:
        """Operator action: act on a quarantined component again."""
        self._quarantined.discard(component)
        self._failed_verifications.pop(component, None)
        self._audit({"event": "reset", "component": component})

    async def tick(self) -> list[dict[str, Any]]:
        if self.mode == "off":
            return []
        records = []
        for probe in self.probes:
            for signal in await self._collect(probe):
                record = await self._handle(probe, signal)
                if record is not None:
                    records.append(record)
        return records

    # ------------------------------------------------------------- internals
    async def _collect(self, probe: Any) -> list[RuntimeSignal]:
        try:
            return list(await probe.collect())
        except Exception as exc:
            name = getattr(probe, "name", type(probe).__name__)
            self._audit({"event": "probe_error", "probe": name, "error_class": type(exc).__name__})
            return []

    async def _handle(self, probe: Any, signal: RuntimeSignal) -> Optional[dict[str, Any]]:
        decision = self.controller.decide(signal)
        component = decision.component
        if decision.action == "none":
            if component in self._quarantined or self._failed_verifications.get(component):
                self._quarantined.discard(component)
                self._failed_verifications.pop(component, None)
                return self._audit({"event": "recovered", "component": component, "decision": decision.to_dict()})
            return None
        base = {"component": component, "decision": decision.to_dict(), "signal": _signal_dict(signal), "mode": self.mode}
        if self.mode == "observe":
            return self._audit({**base, "event": "observed"})
        if decision.action not in HUMAN_ROUTED and not decision.autonomous:
            return self._audit({**base, "event": "needs_human", "reason": "decision is not autonomous"})
        if component in self._quarantined:
            return self._audit({**base, "event": "quarantined", "reason": "earlier actions did not recover this component"})
        blocked = self._budget_block(component, decision)
        if blocked:
            return self._audit({**base, "event": "suppressed", "reason": blocked})
        actuator = next((a for a in self.actuators if decision.action in a.actions), None)
        if actuator is None:
            return self._audit({**base, "event": "no_actuator"})
        self._spend(component)
        try:
            result = await asyncio.wait_for(actuator.execute(decision, signal), timeout=self.action_timeout_seconds)
        except Exception as exc:
            result = ActionResult(False, decision.action, {"error_class": type(exc).__name__, "error": str(exc)[:300]})
        record = {**base, "event": "acted", "result": result.to_dict()}
        if decision.action in HUMAN_ROUTED:
            record["verified"] = None  # a human closes these
            return self._audit(record)
        await self.sleep(self.verify_delay_seconds)
        recovered = await self._verify(probe, component)
        record["verified"] = recovered
        if recovered:
            self._failed_verifications.pop(component, None)
        else:
            self._failed_verifications[component] += 1
            if self._failed_verifications[component] >= self.budget.failed_verifications_before_escalation:
                self._quarantined.add(component)
                record["escalated"] = await self._escalate(decision, signal)
        return self._audit(record)

    async def _verify(self, probe: Any, component: str) -> bool:
        for signal in await self._collect(probe):
            if signal.component == component:
                return self.controller.decide(signal).action == "none"
        return False

    async def _escalate(self, decision: RecoveryDecision, signal: RuntimeSignal) -> bool:
        escalation = RecoveryDecision(
            action="escalate_human_approval",
            component=decision.component,
            reason=f"{decision.action} did not recover {decision.component} after {self.budget.failed_verifications_before_escalation} attempts",
            autonomous=False,
            requires_human_approval=True,
        )
        actuator = next((a for a in self.actuators if "escalate_human_approval" in a.actions), None)
        if actuator is None:
            return False
        try:
            return (await actuator.execute(escalation, signal)).ok
        except Exception:
            return False

    def _budget_block(self, component: str, decision: RecoveryDecision) -> Optional[str]:
        now = self.clock()
        for window in (self._actions[component], self._all_actions):
            while window and now - window[0] > 3600:
                window.popleft()
        last = self._last_action.get(component)
        if last is not None and now - last < decision.cooldown_seconds:
            return f"cooldown {decision.cooldown_seconds}s"
        if len(self._actions[component]) >= self.budget.max_actions_per_component_per_hour:
            return "per-component hourly action limit"
        if len(self._all_actions) >= self.budget.max_actions_per_hour:
            return "global hourly action limit"
        return None

    def _spend(self, component: str) -> None:
        now = self.clock()
        self._actions[component].append(now)
        self._all_actions.append(now)
        self._last_action[component] = now

    def _audit(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"at": self.clock(), **record}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record


def _signal_dict(signal: RuntimeSignal) -> dict[str, Any]:
    return {
        "component": signal.component,
        "health_ok": signal.health_ok,
        "error_rate": signal.error_rate,
        "consecutive_failures": signal.consecutive_failures,
        "safety_violation": signal.safety_violation,
        "metadata": signal.metadata,
    }
