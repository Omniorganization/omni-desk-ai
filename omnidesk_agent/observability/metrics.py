from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Mapping


@dataclass
class MetricsRegistry:
    """Small in-process metrics bridge with Prometheus-compatible names.

    The registry is intentionally dependency-free so production code can publish
    a consistent metrics snapshot even when an external OpenTelemetry or
    Prometheus exporter is not installed in the test/runtime environment. A real
    exporter can scrape `snapshot()` or `render_prometheus()`.
    """

    _values: dict[str, float] = field(default_factory=dict)
    _lock: Any = field(default_factory=Lock)

    def inc(self, name: str, amount: float = 1) -> None:
        self._validate_name(name)
        with self._lock:
            self._values[name] = self._values.get(name, 0.0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        self._validate_name(name)
        with self._lock:
            self._values[name] = float(value)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(sorted(self._values.items()))

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for name, value in self.snapshot().items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value:g}")
        return "\n".join(lines) + ("\n" if lines else "")

    def merge(self, values: Mapping[str, float]) -> None:
        for name, value in values.items():
            self.set_gauge(name, value)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name.startswith("omnidesk_"):
            raise ValueError("metrics must use the omnidesk_ prefix")
        if any(ch.isspace() for ch in name):
            raise ValueError("metric names must not contain whitespace")


DEFAULT_METRICS_REGISTRY = MetricsRegistry()
