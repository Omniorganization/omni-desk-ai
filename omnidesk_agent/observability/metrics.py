from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Mapping


@dataclass
class MetricsRegistry:
    """In-process metrics registry with Prometheus-compatible rendering.

    This registry intentionally preserves the gateway runtime API used by the
    server, orchestrator, BigSeller worker, and tests: counters, gauges,
    histograms, labelled increments, and snapshot access. The dependency-free
    implementation gives CI and local smoke tests the same surface that a real
    exporter would scrape in production.
    """

    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, list[float]] = field(default_factory=dict)
    histogram_buckets: tuple[float, ...] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
    )
    _lock: Lock = field(default_factory=Lock)

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.gauges[key] = float(value)

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        self.set(name, value, **labels)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.histograms.setdefault(key, []).append(float(value))

    def merge(self, values: Mapping[str, float]) -> None:
        for name, value in values.items():
            self.set(name, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "histograms": {key: list(values) for key, values in self.histograms.items()},
            }

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
                if all(
                    f'{label}="{str(val).replace(chr(34), chr(92) + chr(34))}"' in key
                    for label, val in required_labels.items()
                ):
                    total += value
        return total

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self.counters.items()):
                lines.append(f"{key} {value}")
            for key, value in sorted(self.gauges.items()):
                lines.append(f"{key} {value}")
            for key, values in sorted(self.histograms.items()):
                name, labels = self._split_key(key)
                base_labels = dict(labels)
                for bucket in self.histogram_buckets:
                    count = sum(1 for observed in values if observed <= bucket)
                    label_text = self._labels({**base_labels, "le": bucket})
                    lines.append(f"{name}_bucket{label_text} {count}")
                label_text = self._labels({**base_labels, "le": "+Inf"})
                lines.append(f"{name}_bucket{label_text} {len(values)}")
                lines.append(f"{name}_count{self._labels(base_labels)} {len(values)}")
                lines.append(f"{name}_sum{self._labels(base_labels)} {sum(values)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _labels(labels: dict[str, Any]) -> str:
        if not labels:
            return ""
        label_text = ",".join(
            f'{key}="{str(value).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in sorted(labels.items())
        )
        return f"{{{label_text}}}"

    @classmethod
    def _key(cls, name: str, labels: dict[str, Any]) -> str:
        return name + cls._labels(labels)

    @staticmethod
    def _split_key(key: str) -> tuple[str, dict[str, str]]:
        if "{" not in key or not key.endswith("}"):
            return key, {}
        name, raw = key.split("{", 1)
        raw = raw[:-1]
        labels: dict[str, str] = {}
        for part in raw.split(",") if raw else []:
            if "=" in part:
                label, value = part.split("=", 1)
                labels[label] = value.strip('"')
        return name, labels


DEFAULT_METRICS_REGISTRY = MetricsRegistry()
