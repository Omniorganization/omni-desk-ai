"""Alert rules must watch signals that exist and must be actionable.

Why: three shipped rules watched metrics no code ever increments, so they could
never fire while looking like coverage, and no rule had a runbook. These
checks fail when a rule references a gateway metric that nothing emits, when an
alert lacks the severity label Alertmanager routes on, or when its runbook
anchor is missing. promtool (Observability Conformance workflow) checks that the
rules fire; this test checks that they can.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RULE_FILES = [
    ROOT / "deploy" / "observability" / "prometheus-rules.yml",
    ROOT / "deploy" / "observability" / "prometheus-rules.host.yml",
]
RUNBOOK = ROOT / "docs" / "runbooks" / "ALERTS.md"
# Produced outside the gateway: Prometheus itself, node_exporter, and the backup job
# (scripts/verify_backup.py --metrics-file) through node_exporter's textfile collector.
EXTERNAL_PREFIXES = ("up", "node_", "omnidesk_backup_")
PROMQL_WORDS = {
    "sum", "rate", "increase", "by", "without", "and", "or", "unless", "on", "ignoring",
    "abs", "absent", "time", "histogram_quantile", "le", "bool", "max", "min", "avg", "count", "vector",
}


def _rules() -> list[dict]:
    rules: list[dict] = []
    for path in RULE_FILES:
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            rules.extend(group["rules"])
    return rules


def _emitted_metric_names() -> set[str]:
    names: set[str] = set()
    call = re.compile(r"""(?:\binc|_metric|\bobserve|\bset|set_gauge)\(\s*["'](omnidesk_[a-z0-9_]+)["']""")
    mapped = re.compile(r""":\s*["'](omnidesk_[a-z0-9_]+)["']""")
    for path in (ROOT / "omnidesk_agent").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names.update(call.findall(text))
        # Series the registry renders itself (e.g. its dropped-series counter).
        names.update(re.findall(r"# TYPE (omnidesk_[a-z0-9_]+) ", text))
        if "integrations/bigseller/worker.py" in path.as_posix():
            names.update(mapped.findall(text))
    return names


def _metric_names(expr: str, recorded: set[str]) -> set[str]:
    expr = re.sub(r'"[^"]*"', '""', expr)  # drop label values such as "5.." or "/health"
    expr = re.sub(r"\{[^}]*\}", "", expr)
    expr = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", "", expr)
    expr = re.sub(r"\[[^\]]*\]", "", expr)
    tokens = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", expr))
    found = set()
    for token in tokens:
        if token in PROMQL_WORDS or token in recorded or ":" in token:
            continue
        if re.fullmatch(r"\d+[smhdw]?", token):
            continue
        found.add(re.sub(r"_(bucket|count|sum)$", "", token))
    return found


def test_every_alert_watches_a_metric_that_something_emits() -> None:
    rules = _rules()
    recorded = {rule["record"] for rule in rules if "record" in rule}
    emitted = _emitted_metric_names()
    for rule in rules:
        expr = rule["expr"]
        for name in _metric_names(expr, recorded):
            if name.startswith(EXTERNAL_PREFIXES):
                continue
            assert name in emitted, f"{rule.get('alert') or rule.get('record')}: no code emits {name}"
    # The recording rules themselves only use emitted gateway metrics.
    assert {"omnidesk_http_requests_total", "omnidesk_http_errors_total", "omnidesk_http_request_duration_seconds"} <= emitted


def test_removed_rules_for_unemitted_metrics_stay_removed_until_instrumented() -> None:
    emitted = _emitted_metric_names()
    text = "\n".join(path.read_text(encoding="utf-8") for path in RULE_FILES)
    for name in ("omnidesk_sandbox_timeout_total", "omnidesk_self_upgrade_invalid_transition_total"):
        if name not in emitted:
            assert name not in text


def test_backup_alerts_watch_the_series_the_backup_job_writes(tmp_path: Path) -> None:
    """Why: these alerts are the only signal that a restore would fail. If they
    watch names the backup job does not write, Missing pages forever and a failed
    verification never pages."""
    try:
        from omnidesk_agent.storage.postgres_backup import record_verification
    except ImportError:
        pytest.skip("the backup metrics writer (verify_backup.py --metrics-file) lands with #255")
    textfile = tmp_path / "omnidesk_backup.prom"
    record_verification(textfile, kind="sqlite", ok=True)
    record_verification(textfile, kind="postgres", ok=False)
    written = set(re.findall(r"^(omnidesk_backup_[a-z0-9_]+)\{", textfile.read_text(encoding="utf-8"), re.M))
    watched = {name for rule in _rules() for name in re.findall(r"omnidesk_backup_[a-z0-9_]+", rule["expr"])}
    assert watched and watched <= written, (watched, written)


def test_alerts_are_routable_and_have_runbooks() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    anchors = {line[3:].strip().lower() for line in runbook.splitlines() if line.startswith("## ")}
    names = []
    for rule in _rules():
        if "alert" not in rule:
            continue
        names.append(rule["alert"])
        assert rule["labels"]["severity"] in {"critical", "warning"}, rule["alert"]
        assert rule["annotations"].get("summary"), rule["alert"]
        if rule["alert"].startswith("OmniDeskBigSeller"):
            continue
        url = rule["annotations"].get("runbook_url", "")
        assert url.endswith("#" + rule["alert"].lower()), rule["alert"]
        assert rule["alert"].lower() in anchors, f"docs/runbooks/ALERTS.md has no section for {rule['alert']}"
    assert len(names) == len(set(names)), "duplicate alert names"
    # The SLO alerts the audit found missing exist.
    for required in ("OmniDeskGatewayDown", "OmniDeskAvailabilityBudgetFastBurn", "OmniDeskLatencyBudgetFastBurn"):
        assert required in names


def test_every_alert_has_a_promtool_case() -> None:
    tests = yaml.safe_load((ROOT / "deploy" / "observability" / "prometheus-rules.test.yml").read_text(encoding="utf-8"))
    tested = {case["alertname"] for test in tests["tests"] for case in test.get("alert_rule_test", [])}
    untested = [rule["alert"] for rule in _rules() if "alert" in rule and rule["alert"] not in tested]
    # Pre-existing pass-through counters keep their original rules; every new
    # SLO, telemetry and host alert must be exercised by promtool.
    legacy = {
        "OmniDeskApprovalResumeFailures", "OmniDeskOutboundAmbiguousSend", "OmniDeskOutboundDeadLetterRate",
        "OmniDeskJobsDeadLettered", "OmniDeskWebhookSignatureFailures", "OmniDeskRequestsThrottled",
        "OmniDeskTraceSpanFailures", "OmniDeskBigSellerWebhookRejected", "OmniDeskBigSellerWebhookDuplicates",
        "OmniDeskBigSellerDeadLetterCurrent", "OmniDeskNodeExporterDown", "OmniDeskHostDiskFillingUp",
        "OmniDeskHostDiskCritical", "OmniDeskHostMemoryLow",
    }
    assert set(untested) <= legacy, f"alerts without a promtool case: {sorted(set(untested) - legacy)}"


def test_slack_receiver_reads_the_webhook_url_from_a_file_only() -> None:
    # The incoming-webhook URL is a bearer secret: it must never be written into
    # the rendered config (which is mounted read-only and may be logged).
    import importlib.util

    spec = importlib.util.spec_from_file_location("render_alertmanager_config", ROOT / "scripts" / "render_alertmanager_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = yaml.safe_load(module.render("slack", slack_url_file="/etc/alertmanager/secrets/slack_webhook_url", slack_channel="#omnidesk-alerts"))
    (receiver,) = rendered["receivers"]
    (slack,) = receiver["slack_configs"]
    assert slack["api_url_file"] == "/etc/alertmanager/secrets/slack_webhook_url"
    assert "api_url" not in slack and slack["send_resolved"] is True
    assert rendered["route"]["receiver"] == receiver["name"] == "omnidesk-oncall"
    for bad in ({"slack_url_file": "relative/path"}, {"slack_url_file": "/ok", "slack_channel": "general; rm"}):
        try:
            module.render("slack", **bad)
        except ValueError:
            continue
        raise AssertionError(f"render accepted {bad}")
