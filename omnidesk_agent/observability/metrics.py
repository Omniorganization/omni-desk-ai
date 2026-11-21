from __future__ import annotations

from dataclasses import dataclass, field
import math
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
    max_series: int = 2048
    max_samples_per_series: int = 64
    _histogram_totals: dict[str, tuple[int, float, list[int]]] = field(default_factory=dict)
    _histogram_families: set[str] = field(default_factory=set)
    dropped_series_updates: int = 0

    def _admit_key(self, values: Mapping[str, Any], key: str) -> bool:
        # Reject rather than reinterpret oversized/overflow labels. Admission is
        # under _lock and global across all metric types, including trace labels.
        if len(key) > 2048 or (key not in values and len(self.counters) + len(self.gauges) + len(self.histograms) >= self.max_series):
            self.dropped_series_updates += 1
            return False
        return True

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            if not self._admit_key(self.counters, key):
                return
            self.counters[key] = self.counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            if not self._admit_key(self.gauges, key):
                return
            self.gauges[key] = float(value)

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        self.set(name, value, **labels)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        value = float(value)
        if not math.isfinite(value):
            return
        with self._lock:
            if not self._admit_key(self.histograms, key):
                return
            samples = self.histograms.setdefault(key, [])
            count, total, buckets = self._histogram_totals.get(key, (0, 0.0, [0] * len(self.histogram_buckets)))
            for index, bound in enumerate(self.histogram_buckets):
                if value <= bound:
                    buckets[index] += 1
            self._histogram_totals[key] = (count + 1, total + value, buckets)
            # Snapshot diagnostic samples remain compatible but bounded;
            # Prometheus aggregates below retain ALL observations exactly.
            if self.max_samples_per_series > 0:
                samples.append(value)
                del samples[:-self.max_samples_per_series]

    def declare_histogram(self, name: str) -> None:
        """Expose ``name`` as an (empty) histogram family before its first observation."""
        with self._lock:
            self._histogram_families.add(name)

    def merge(self, values: Mapping[str, float]) -> None:
        for name, value in values.items():
            self.set(name, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "histograms": {key: list(values) for key, values in self.histograms.items()},
                "histogram_totals": {key: {"count": count, "sum": total, "buckets": list(buckets)} for key, (count, total, buckets) in self._histogram_totals.items()},
                "dropped_series_updates": self.dropped_series_updates,
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
        """Render Prometheus text exposition format 0.0.4.

        Every family gets exactly one ``# TYPE`` line. A name is a histogram if
        it was declared or observed, otherwise a counter if it was incremented,
        otherwise a gauge; series of the same family recorded with another type
        are left out (and counted as dropped) instead of producing an invalid
        exposition that Prometheus would refuse to ingest.
        """
        with self._lock:
            histogram_families = set(self._histogram_families)
            for key in self._histogram_totals:
                histogram_families.add(self._split_key(key)[0])
            counter_families: dict[str, list[tuple[str, float]]] = {}
            gauge_families: dict[str, list[tuple[str, float]]] = {}
            conflicts = 0
            for key, value in self.counters.items():
                family = self._split_key(key)[0]
                if family in histogram_families:
                    conflicts += 1 if value else 0
                    continue
                counter_families.setdefault(family, []).append((key, value))
            for key, value in self.gauges.items():
                family = self._split_key(key)[0]
                if family in histogram_families or family in counter_families:
                    conflicts += 1
                    continue
                gauge_families.setdefault(family, []).append((key, value))
            dropped = self.dropped_series_updates
            histograms: dict[str, list[tuple[dict[str, str], int, float, list[int]]]] = {name: [] for name in histogram_families}
            for key, (count, total, buckets) in self._histogram_totals.items():
                name, labels = self._split_key(key)
                histograms[name].append((labels, count, total, list(buckets)))
        lines: list[str] = []
        for family in sorted(counter_families):
            lines.append(f"# TYPE {family} counter")
            lines.extend(f"{key} {value}" for key, value in sorted(counter_families[family]))
        for family in sorted(gauge_families):
            lines.append(f"# TYPE {family} gauge")
            lines.extend(f"{key} {value}" for key, value in sorted(gauge_families[family]))
        for family in sorted(histograms):
            lines.append(f"# TYPE {family} histogram")
            series = histograms[family] or [({}, 0, 0.0, [0] * len(self.histogram_buckets))]
            for labels, count, total, buckets in sorted(series, key=lambda item: sorted(item[0].items())):
                for bucket, bucket_count in zip(self.histogram_buckets, buckets):
                    lines.append(f"{family}_bucket{self._labels({**labels, 'le': bucket})} {bucket_count}")
                lines.append(f"{family}_bucket{self._labels({**labels, 'le': '+Inf'})} {count}")
                lines.append(f"{family}_count{self._labels(labels)} {count}")
                lines.append(f"{family}_sum{self._labels(labels)} {total}")
        if "omnidesk_metrics_dropped_series_updates_total" not in counter_families:
            lines.append("# TYPE omnidesk_metrics_dropped_series_updates_total counter")
            lines.append(f"omnidesk_metrics_dropped_series_updates_total {float(dropped + conflicts)}")
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
