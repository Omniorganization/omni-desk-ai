from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from omnidesk_agent.models.cost_store import ModelCostStore


@dataclass
class ModelCostEntry:
    task_id: str
    task: str
    profile: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


@dataclass
class ModelCostLedger:
    """Lightweight per-runtime model usage ledger.

    Prices are intentionally not hard-coded. Providers can pass cost metadata in
    usage; otherwise the ledger still provides per-task token attribution.
    """

    entries: list[ModelCostEntry] = field(default_factory=list)
    store: Optional[ModelCostStore] = None

    def record(self, *, task_id: str, task: str, profile: str, model: str, provider: str, usage: Optional[dict[str, Any]], estimated_output_tokens: int = 0) -> None:
        usage = usage or {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("estimated_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("estimated_output_tokens") or estimated_output_tokens or 0)
        cost = float(usage.get("cost_usd") or usage.get("estimated_cost_usd") or 0.0)
        self.entries.append(ModelCostEntry(task_id=task_id, task=task, profile=profile, model=model, provider=provider, input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost=cost))
        if self.store is not None:
            self.store.record(task_id=task_id, task=task, profile=profile, model=model, provider=provider, input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=cost, cache_hit=bool(usage.get("cache_hit")), run_id=usage.get("run_id"), actor=usage.get("actor"))

    def summary(self, *, task_id: Optional[str] = None) -> dict[str, Any]:
        entries = [e for e in self.entries if task_id is None or e.task_id == task_id]
        durable = self.store.summary() if self.store is not None and task_id is None else None
        return {
            "calls": len(entries),
            "input_tokens": sum(e.input_tokens for e in entries),
            "output_tokens": sum(e.output_tokens for e in entries),
            "estimated_cost": sum(e.estimated_cost for e in entries),
            "by_profile": self._by_profile(entries),
            "durable": durable,
        }

    @staticmethod
    def _by_profile(entries: list[ModelCostEntry]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for e in entries:
            row = out.setdefault(e.profile, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0})
            row["calls"] += 1
            row["input_tokens"] += e.input_tokens
            row["output_tokens"] += e.output_tokens
            row["estimated_cost"] += e.estimated_cost
        return out

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
