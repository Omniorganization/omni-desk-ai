"""The drill report must pass the external GA evidence gate only when the real
run contained every injection and health passed afterwards."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

from omnidesk_agent.self_healing.drill import run_drill

ROOT = Path(__file__).resolve().parents[1]


def _gate():
    spec = importlib.util.spec_from_file_location("ga_evidence", ROOT / "scripts" / "check_external_ga_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(audit: Path):
    return {
        "environment": "staging",
        "health_url": "https://staging.invalid/health",
        "injections": [{"name": "bad-config", "inject": [["deploy", "bad.yaml"]], "audit_log": str(audit), "event": "recovered", "timeout_seconds": 30}],
    }


def _evaluate(evidence_dir: Path, report: dict) -> list[str]:
    gate = _gate()
    path = evidence_dir / "drills" / "self-healing-failure-injection.json"
    path.write_text(json.dumps(report))
    doc, issues = gate._validate_common(ROOT, evidence_dir, path, {"requires_artifact": True})
    return issues + gate._category_specific("self_healing_failure_injection", doc)


def test_a_contained_drill_produces_evidence_the_gate_accepts(tmp_path):
    evidence_dir = tmp_path / "external-evidence"
    audit = tmp_path / "watchdog.jsonl"
    clock = {"now": 1_000.0}

    def run(argv):  # the injected fault; the watchdog then writes its record
        clock["now"] += 5
        audit.write_text(json.dumps({"at": clock["now"] + 20, "event": "recovered", "containment_action": "rollback_config_and_restart"}) + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    report = run_drill(_plan(audit), out_dir=evidence_dir / "drills", run=run, health=lambda url: {"ok": True, "status_code": 200},
                       clock=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s))

    assert report["status"] == "passed" and report["recovery_verified"] is True
    assert report["containment_action"] == "rollback_config_and_restart"
    assert report["failure_injections"][0]["seconds_to_containment"] == 25.0  # from injection start
    assert _evaluate(evidence_dir, report) == []


def test_an_uncontained_drill_fails_and_the_gate_rejects_it(tmp_path):
    evidence_dir = tmp_path / "external-evidence"
    audit = tmp_path / "watchdog.jsonl"
    clock = {"now": 1_000.0}
    report = run_drill(_plan(audit), out_dir=evidence_dir / "drills", run=lambda argv: subprocess.CompletedProcess(argv, 0, "", ""),
                       health=lambda url: {"ok": False, "error_class": "URLError"},
                       clock=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s))

    assert report["status"] == "failed" and report["recovery_verified"] is False
    issues = _evaluate(evidence_dir, report)
    assert "recovery_verified must be true" in issues and "status must be passed/succeeded/verified" in issues


def test_an_unrelated_or_failed_supervisor_record_does_not_count_as_containment(tmp_path):
    evidence_dir = tmp_path / "external-evidence"
    audit = tmp_path / "healing.jsonl"
    clock = {"now": 1_000.0}
    plan = {"environment": "staging", "health_url": "https://staging.invalid/health", "injections": [
        {"name": "kill-worker", "inject": [["kill-worker"]], "audit_log": str(audit), "event": "acted",
         "component": "worker:webhook", "action": "restart_worker", "timeout_seconds": 10}]}

    def run(argv):
        audit.write_text(
            json.dumps({"at": clock["now"] + 1, "event": "acted", "component": "queue:dead_letters",
                        "decision": {"action": "requeue_dead_letters"}, "result": {"ok": True}, "verified": True}) + "\n"
            + json.dumps({"at": clock["now"] + 2, "event": "acted", "component": "worker:webhook",
                          "decision": {"action": "restart_worker"}, "result": {"ok": False}, "verified": False}) + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    report = run_drill(plan, out_dir=evidence_dir / "drills", run=run, health=lambda url: {"ok": True},
                       clock=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s))
    assert report["status"] == "failed" and report["failure_injections"][0]["contained"] is False


def test_supervisor_injections_must_name_their_component(tmp_path):
    import pytest

    plan = {"health_url": "https://staging.invalid/health", "injections": [{"name": "x", "inject": [["true"]], "audit_log": str(tmp_path / "a"), "event": "acted"}]}
    with pytest.raises(ValueError, match="must name the component"):
        run_drill(plan, out_dir=tmp_path, run=lambda argv: subprocess.CompletedProcess(argv, 0, "", ""), health=lambda url: {"ok": True})
