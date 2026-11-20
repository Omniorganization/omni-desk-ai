#!/usr/bin/env python3
"""Soak a running OmniDesk gateway through its real HTTP API.

Unlike scripts/soak_test.py (deterministic in-process checks), this drives a
live deployment for a fixed duration with concurrent clients and checks:

* availability: no unexpected 5xx / transport errors (probes, admin reads,
  agent runs), with per-route latency percentiles;
* idempotency under concurrency: replaying an /agent/run idempotency key with
  the same payload returns the same response, and reusing it with a different
  payload is rejected (409) instead of running twice;
* the gateway's own telemetry: periodic /admin/metrics scrapes must parse,
  the series cap must not drop updates, and the post-deploy SLO gate
  (scripts/production_smoke_test.py) must hold at the end.

Agent runs use the configured planner and model, at --agent-runs-per-minute
(kept under the per-actor quota). Each run may cost model spend on a
deployment with a provider configured; staging caps that with its model
budget (block on exceed).

Secrets are read from environment variables named by flags and never printed.
Exit 0 only when every invariant held; the JSON report goes to --report.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_smoke_test import _slo_gate  # noqa: E402


class Client:
    def __init__(self, base_url: str, admin_token: str, gateway_secret: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.gateway_secret = gateway_secret
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None, auth: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"content-type": "application/json", "x-request-id": str(uuid.uuid4())}
        if auth:
            request_headers["authorization"] = f"Bearer {self.admin_token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=data, headers=request_headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310 - operator-provided URL
                payload = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = int(exc.code)
            exc.close()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return 0, {"transport_error": type(exc).__name__}, time.perf_counter() - started
        try:
            parsed = json.loads(payload or b"{}")
        except ValueError:
            parsed = {"raw": payload[:2000].decode("utf-8", "replace")}
        return status, parsed, time.perf_counter() - started


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.latency = defaultdict(list)
        self.status = defaultdict(lambda: defaultdict(int))
        self.violations: list[dict] = []
        self.keys: list[tuple[str, str, dict]] = []

    def record(self, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.latency[route].append(seconds)
            self.status[route][str(status)] += 1

    def violation(self, kind: str, **details) -> None:
        with self.lock:
            if len(self.violations) < 200:
                self.violations.append({"kind": kind, "at": round(time.time(), 3), **details})


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 4)


def _agent_body(client: Client, message: str) -> dict:
    body = {"message": message}
    if client.gateway_secret:
        body["secret"] = client.gateway_secret
    return body


class AgentBudget:
    """Token bucket that keeps agent runs within the per-actor quota (default 20/min)."""

    def __init__(self, per_minute: float):
        self.rate = per_minute / 60.0
        self.tokens = min(1.0, per_minute)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(max(1.0, self.rate * 10), self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.rate > 0 and self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


def _one_operation(client: Client, recorder: Recorder, rng: random.Random, sequence: int, agent_budget: AgentBudget) -> None:
    if agent_budget.take():
        with recorder.lock:
            sample = rng.choice(recorder.keys) if recorder.keys else None
        if sample is not None and rng.random() < 0.3:
            key, message, first = sample
            if rng.random() < 0.6:
                status, payload, seconds = client.request("POST", "/agent/run", _agent_body(client, message), {"idempotency-key": key}, auth=True)
                recorder.record("/agent/run#replay", status, seconds)
                if status != 200 or payload != first:
                    recorder.violation("idempotent_replay_mismatch", status=status)
            else:
                status, _, seconds = client.request("POST", "/agent/run", _agent_body(client, message + " (changed)"), {"idempotency-key": key}, auth=True)
                recorder.record("/agent/run#conflict", status, seconds)
                if status != 409:
                    recorder.violation("idempotency_conflict_not_rejected", status=status)
            return
        key = f"soak-{uuid.uuid4()}"
        body = _agent_body(client, f"soak check {sequence}: report status")
        status, payload, seconds = client.request("POST", "/agent/run", body, {"idempotency-key": key}, auth=True)
        recorder.record("/agent/run", status, seconds)
        if status >= 500 or status == 0:
            recorder.violation("agent_run_server_error", status=status, detail=str(payload)[:300])
        elif status == 200:
            with recorder.lock:
                if len(recorder.keys) < 5000:
                    recorder.keys.append((key, body["message"], payload))
        return
    if rng.random() < 0.6:
        path = rng.choice(["/health", "/ready"])
        status, _, seconds = client.request("GET", path)
        recorder.record(path, status, seconds)
        if status != 200:
            recorder.violation("probe_not_ok", path=path, status=status)
    else:
        status, _, seconds = client.request("GET", "/admin/status", auth=True)
        recorder.record("/admin/status", status, seconds)
        if status not in (200, 429):
            recorder.violation("admin_read_failed", status=status)


METRIC_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (-?[0-9.eE+-]+|NaN|\+Inf|-Inf)$")


def _scrape(client: Client) -> tuple[bool, dict[str, float]]:
    request = urllib.request.Request(client.base_url + "/admin/metrics", headers={"authorization": f"Bearer {client.admin_token}"})
    try:
        with urllib.request.urlopen(request, timeout=client.timeout) as response:  # nosec B310
            text = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return False, {}
    totals: dict[str, float] = defaultdict(float)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC_LINE.match(line)
        if not match:
            return False, {}
        name, labels, value = match.groups()
        if name in {"omnidesk_metrics_dropped_series_updates_total", "omnidesk_trace_export_failures_total"}:
            totals[name] += float(value)
        if name == "omnidesk_http_requests_total" and labels and 'status="5' in labels:
            totals["http_5xx"] += float(value)
    return True, dict(totals)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token-env", default="OMNIDESK_SOAK_ADMIN_TOKEN")
    parser.add_argument("--gateway-secret-env", default="OMNIDESK_SOAK_GATEWAY_SECRET")
    parser.add_argument("--duration-seconds", type=int, default=600)
    parser.add_argument("--concurrency", type=int, default=8)
    # One client IP: the gateway allows 300 requests/min per IP and 120/min per IP and
    # route (api_resource_guard). The default stays under both so a 429 means a real problem.
    parser.add_argument("--rate-per-second", type=float, default=4.0, help="target total request rate")
    parser.add_argument("--agent-runs-per-minute", type=float, default=6.0,
                        help="agent runs per minute, kept under the per-actor quota (0 disables them)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-error-ratio", type=float, default=0.005, help="SLO: 99.5%% availability")
    parser.add_argument("--max-p95-control-plane-seconds", type=float, default=1.0)
    parser.add_argument("--environment", default="ci")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261121)
    args = parser.parse_args(argv)
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an http(s) URL")
    client = Client(args.base_url, os.getenv(args.admin_token_env, ""), os.getenv(args.gateway_secret_env, ""), args.timeout)
    if not client.admin_token:
        parser.error(f"{args.admin_token_env} must hold an operator/admin token")
    recorder = Recorder()
    started_at = time.time()
    deadline = time.monotonic() + args.duration_seconds
    interval = max(1, args.concurrency) / max(args.rate_per_second, 0.1)
    scrapes: list[dict] = []
    sequence = iter(range(10**9))
    agent_budget = AgentBudget(args.agent_runs_per_minute)

    def worker(index: int) -> None:
        rng = random.Random(args.seed + index)
        while time.monotonic() < deadline:
            tick = time.monotonic()
            _one_operation(client, recorder, rng, next(sequence), agent_budget)
            time.sleep(max(0.0, interval - (time.monotonic() - tick)))

    def scraper() -> None:
        while time.monotonic() < deadline:
            ok, totals = _scrape(client)
            scrapes.append({"at": round(time.time(), 3), "ok": ok, **totals})
            if not ok:
                recorder.violation("metrics_scrape_failed")
            time.sleep(min(30.0, max(1.0, deadline - time.monotonic())))

    with ThreadPoolExecutor(max_workers=args.concurrency + 1) as pool:
        futures = [pool.submit(worker, i) for i in range(args.concurrency)] + [pool.submit(scraper)]
        for future in futures:
            future.result()

    status_code, slo_payload, _ = client.request("GET", "/admin/slo", auth=True)
    if status_code == 429:
        time.sleep(61)  # let the per-IP window drain, then read the gate once more
        status_code, slo_payload, _ = client.request("GET", "/admin/slo", auth=True)
    gate = _slo_gate(slo_payload) if status_code == 200 else {"ok": False, "blocking": [{"metric": "<admin/slo>", "status": status_code}]}
    routes = {}
    total = errors = throttled = 0
    for route, values in recorder.latency.items():
        statuses = dict(recorder.status[route])
        failed = sum(count for code, count in statuses.items() if code == "0" or code.startswith("5"))
        # 429 is the gateway enforcing its quotas, not an outage; it is reported, not counted as an error.
        throttled += statuses.get("429", 0)
        total += len(values)
        errors += failed
        routes[route] = {"requests": len(values), "status": statuses, "p50": _percentile(values, 0.5),
                         "p95": _percentile(values, 0.95), "p99": _percentile(values, 0.99),
                         "mean": round(statistics.fmean(values), 4)}
    dropped = [s.get("omnidesk_metrics_dropped_series_updates_total", 0.0) for s in scrapes if s.get("ok")]
    control_plane_p95 = max((routes[r]["p95"] for r in ("/health", "/ready", "/admin/status") if r in routes), default=0.0)
    error_ratio = errors / total if total else 1.0
    checks = {
        "requests_made": total > 0,
        "availability_within_slo": error_ratio <= args.max_error_ratio,
        "control_plane_p95_within_slo": control_plane_p95 <= args.max_p95_control_plane_seconds,
        "no_invariant_violations": not recorder.violations,
        "metrics_series_not_dropped": not dropped or dropped[-1] == dropped[0],
        "slo_gate_ok": bool(gate.get("ok")),
    }
    report = {
        "schema": "omnidesk-product-soak/v1",
        "environment": args.environment,
        "target_host": parsed.hostname,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "duration_seconds": round(time.time() - started_at, 1),
        "concurrency": args.concurrency,
        "requests": total,
        "errors": errors,
        "throttled": throttled,
        "agent_runs_per_minute": args.agent_runs_per_minute,
        "error_ratio": round(error_ratio, 6),
        "routes": routes,
        "idempotency_keys_exercised": len(recorder.keys),
        "violations": recorder.violations,
        "metrics_scrapes": len(scrapes),
        "slo_gate": gate,
        "checks": checks,
        "ok": all(checks.values()),
        "workflow_run": os.getenv("GITHUB_RUN_ID") and f"{os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.getenv('GITHUB_RUN_ID')}",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "requests", "errors", "error_ratio", "checks")}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
