"""Run a controlled failure-injection drill on a real host and record what happened.

    python -m omnidesk_agent.self_healing.drill --plan drill-plan.json --out evidence.json

The plan (JSON) names a health URL and a list of injections. Each injection has
``inject`` (a list of argv lists, run without a shell), the self-healing audit
log to watch (``audit_log``), the audit ``event`` that proves containment (for
example ``recovered`` from the watchdog or ``acted`` from the supervisor), the
``component`` it breaks (required for supervisor logs), an optional ``action``,
and a ``timeout_seconds``. Only a record for that component whose action
succeeded and was verified counts.

Before every injection health must pass ``settle_checks`` times in a row,
``settle_seconds`` apart; otherwise that injection and the rest of the plan
are skipped and the drill fails. An injection counts only when every inject
command exited 0, and a record contains it only when the failure the record
answered began after the injection did (the healers write
``first_failure_at`` / ``first_unhealthy_at``). Injections run one after
another, so a record found for one cannot answer a failure that began after
the next one started. Exit code 0 alone does not prove that a fault took
effect.
The runner records only what it observes: command exit codes, health checks,
the first matching audit record, and post-recovery health checks. ``status``
is ``passed`` only when every injection was injected and contained and health
passed afterwards; otherwise it is ``failed``. The output follows the external GA evidence schema for
``self_healing_failure_injection``; the audit log excerpts are attached with
their SHA-256.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import socket
import subprocess  # nosec B404
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

PRODUCER = "omnidesk-self-healing-drill/v1"


def _health(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": PRODUCER}), timeout=timeout) as response:  # nosec B310
            return {"ok": 200 <= response.status < 300, "status_code": response.status}
    except Exception as exc:
        return {"ok": False, "error_class": type(exc).__name__}


def _audit_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def run_drill(
    plan: dict[str, Any],
    *,
    out_dir: Path,
    health: Callable[[str], dict[str, Any]] = _health,
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False),  # nosec B603
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    health_url = str(plan["health_url"])
    out_dir.mkdir(parents=True, exist_ok=True)
    injections: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for spec in plan["injections"]:
        if str(spec.get("event")) == "acted" and not spec.get("component"):
            raise ValueError("a supervisor-log injection must name the component it breaks")
        if not spec["inject"] or not all(isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) for argv in spec["inject"]):
            raise ValueError("each inject step must be a non-empty argv list of strings")
    settle_checks = max(1, int(plan.get("settle_checks", 3)))
    settle_seconds = float(plan.get("settle_seconds", 10))
    baseline: Optional[dict[str, Any]] = None
    stopped = False
    for spec in plan["injections"]:
        checks: list[dict[str, Any]] = []
        if not stopped:  # never inject into a system that is not steadily healthy
            for attempt in range(settle_checks):
                if attempt:
                    sleep(settle_seconds)
                checks.append(health(health_url))
                if checks[-1].get("ok") is not True:
                    break
        baseline = baseline if baseline is not None else (checks[0] if checks else None)
        baseline_ok = len(checks) == settle_checks and all(check.get("ok") is True for check in checks)
        stopped = stopped or not baseline_ok
        started = clock()
        commands = []
        if baseline_ok:
            for argv in spec["inject"]:
                proc = run(argv)
                commands.append({"argv": argv, "exit_code": proc.returncode, "stderr_tail": (proc.stderr or "")[-300:]})
        injected = baseline_ok and all(command["exit_code"] == 0 for command in commands)
        audit_path = Path(spec["audit_log"])
        wanted = str(spec["event"])
        deadline = started + float(spec.get("timeout_seconds", 300))
        match: Optional[dict[str, Any]] = None
        while injected and clock() < deadline and match is None:
            match = next((r for r in _audit_records(audit_path)
                          if float(r.get("at", 0)) >= started and _began_after(r, started) and _contains(r, spec, wanted)), None)
            if match is None:
                sleep(2.0)
        contained = match is not None
        excerpt = [r for r in _audit_records(audit_path) if float(r.get("at", 0)) >= started]
        excerpt_path = out_dir / f"{spec['name']}-audit.jsonl"
        excerpt_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in excerpt), encoding="utf-8")
        # Paths are relative to the evidence directory, e.g. "drills/<name>-audit.jsonl".
        artifacts.append({"path": f"{out_dir.name}/{excerpt_path.name}", "sha256": hashlib.sha256(excerpt_path.read_bytes()).hexdigest()})
        injections.append({
            "name": spec["name"],
            "description": spec.get("description", ""),
            "injected_at": _iso(started),
            "baseline_checks": checks,
            "commands": commands,
            "injected": injected,
            "skipped": None if baseline_ok else ("baseline health check failed" if checks else "not run: an earlier baseline failed"),
            "contained": contained,
            "containment_record": match,
            "seconds_to_containment": round(float(match["at"]) - started, 3) if match else None,
        })
    post = [health(health_url) for _ in range(int(plan.get("post_checks", 3)))]
    recovered = all(i["contained"] for i in injections) and bool(injections)
    healthy_after = all(check["ok"] for check in post)
    actions = sorted({str((i["containment_record"] or {}).get("containment_action") or (i["containment_record"] or {}).get("decision", {}).get("action") or "") for i in injections if i["contained"]} - {""})
    return {
        "schema": "omnidesk-self-healing-drill/v1",
        "status": "passed" if recovered and healthy_after else "failed",
        "produced_at": _iso(clock()),
        "producer": f"{PRODUCER} on {socket.gethostname()}",
        "environment": str(plan.get("environment", "")),
        "health_url": health_url,
        "baseline_health": baseline,
        "failure_injections": injections,
        "containment_action": ", ".join(actions),
        "recovery_verified": recovered,
        "post_recovery_health": "passed" if healthy_after else "failed",
        "post_recovery_checks": post,
        "artifacts": artifacts,
    }


def _began_after(record: dict[str, Any], started: float) -> bool:
    """The failure this record answered was first seen at or after the injection."""
    first = record.get("first_failure_at", record.get("first_unhealthy_at"))
    return first is not None and float(first) >= started


def _contains(record: dict[str, Any], spec: dict[str, Any], wanted: str) -> bool:
    """A record proves containment only for the injected component and only if it succeeded."""
    if record.get("event") != wanted:
        return False
    if spec.get("component") and record.get("component") != spec["component"]:
        return False
    action = spec.get("action")
    if action and (record.get("decision") or {}).get("action") != action and record.get("containment_action") != action:
        return False
    if "verified" in record and record.get("verified") is not True:
        return False
    if (record.get("result") or {}).get("ok") is False:
        return False
    return True


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.self_healing.drill")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True, help="evidence JSON path; audit excerpts are written next to it")
    args = parser.parse_args(argv)
    out = Path(args.out)
    report = run_drill(json.loads(Path(args.plan).read_text(encoding="utf-8")), out_dir=out.parent)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "out": str(out)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
