"""The desktop npm audit may accept an advisory only through a governed exception record.

Every other high or critical advisory, an expired or out-of-scope record, a record that
runs too far ahead, or an unreadable report must still fail the Security workflow.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from scripts.check_npm_audit import check, main
from scripts.check_security_exceptions import main as check_exception_records

ROOT = Path(__file__).resolve().parents[1]
GHSA = "GHSA-fc5q-39xc-vjpf"
OTHER = "GHSA-aaaa-bbbb-cccc"
TODAY = date(2026, 11, 18)
PROJECT = "apps/desktop-tauri"


def _advisory(ghsa: str, severity: str = "high") -> dict:
    return {"source": 1, "name": "picomatch", "title": "ReDoS", "url": f"https://github.com/advisories/{ghsa}",
            "severity": severity, "range": "<=4.0.7"}


def _report(*advisories: dict) -> dict:
    # Shape of `npm audit --json` (npm 11): the root package carries the advisory object,
    # packages that depend on it only name that package.
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "picomatch": {"name": "picomatch", "severity": "high", "via": list(advisories)},
            "fdir": {"name": "fdir", "severity": "high", "via": ["picomatch"]},
            "vite": {"name": "vite", "severity": "high", "via": ["picomatch", "tinyglobby"]},
        },
        "metadata": {"vulnerabilities": {"high": 3, "critical": 0}},
    }


def _record(directory: Path, ghsa: str = GHSA, *, scope: str = f"npm {PROJECT} build only",
            expires_at: str = "2027-02-01", status: str = "active") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ghsa}.md"
    path.write_text(
        "\n".join([
            f"Exception ID: {ghsa}", f"Status: {status}", "Owner: security-owner", f"Scope: {scope}",
            f"Expires At: {expires_at}", "Upstream Tracking: upstream PR 254",
            "Compensating Controls: every other advisory still blocks",
            "## Impact", "x", "## Runtime reachability", "x", "## Compensating control", "x", "## Removal criteria", "x",
        ]) + "\n",
        encoding="utf-8",
    )
    return path


def test_governed_exception_accepts_only_its_own_advisory(tmp_path):
    _record(tmp_path)
    assert check(_report(_advisory(GHSA)), PROJECT, tmp_path, TODAY) == ([GHSA], [])
    accepted, issues = check(_report(_advisory(GHSA), _advisory(OTHER, "critical")), PROJECT, tmp_path, TODAY)
    assert accepted == [GHSA]
    assert any(OTHER in issue for issue in issues)


def test_advisory_without_a_record_blocks(tmp_path):
    accepted, issues = check(_report(_advisory(GHSA)), PROJECT, tmp_path, TODAY)
    assert accepted == [] and any("no governed exception record" in issue for issue in issues)


def test_moderate_and_low_advisories_do_not_need_a_record(tmp_path):
    report = {"auditReportVersion": 2,
              "vulnerabilities": {"picomatch": {"severity": "moderate", "via": [_advisory(OTHER, "moderate")]},
                                  "fdir": {"severity": "low", "via": [_advisory(GHSA, "low")]}},
              "metadata": {"vulnerabilities": {"moderate": 1, "low": 1, "high": 0, "critical": 0}}}
    assert check(report, PROJECT, tmp_path, TODAY) == ([], [])


def test_record_for_another_project_does_not_cover_this_one(tmp_path):
    _record(tmp_path, scope="npm apps/desktop-tauri-legacy build only")
    accepted, issues = check(_report(_advisory(GHSA)), PROJECT, tmp_path, TODAY)
    assert accepted == [] and any("scope does not name" in issue for issue in issues)
    _record(tmp_path, scope="npm apps/web-admin-next")
    assert check(_report(_advisory(GHSA)), PROJECT, tmp_path, TODAY)[0] == []


def test_expired_inactive_or_overlong_records_block(tmp_path):
    for kwargs, expected in (
        ({"expires_at": "2026-11-17"}, "expired"),
        ({"status": "mitigated"}, "must be active"),
        ({"expires_at": "2027-03-01"}, "at most 90 days"),
    ):
        _record(tmp_path, **kwargs)
        accepted, issues = check(_report(_advisory(GHSA)), PROJECT, tmp_path, TODAY)
        assert accepted == [] and any(expected in issue for issue in issues), kwargs


def test_unreadable_or_unexplained_reports_fail_closed(tmp_path):
    _record(tmp_path)
    for report in ({"error": {"code": "ENOAUDIT"}}, {"auditReportVersion": 1, "vulnerabilities": {}}, [], None):
        assert check(report, PROJECT, tmp_path, TODAY)[1]
    unexplained = {"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {"vulnerabilities": {"high": 1}}}
    assert check(unexplained, PROJECT, tmp_path, TODAY)[1]
    no_ghsa = _report({**_advisory(GHSA), "url": "https://example.invalid/advisory"})
    assert check(no_ghsa, PROJECT, tmp_path, TODAY)[1]


def test_cli_fails_on_a_missing_report(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json"), "--project", PROJECT, "--exceptions", str(tmp_path)]) == 1
    assert "cannot read npm audit report" in capsys.readouterr().err


def test_cli_accepts_a_valid_record_and_refuses_one_beyond_90_days(tmp_path, capsys):
    report = tmp_path / "audit.json"
    report.write_text(json.dumps(_report(_advisory(GHSA))), encoding="utf-8")
    _record(tmp_path, expires_at=(date.today() + timedelta(days=60)).isoformat())
    assert main([str(report), "--project", PROJECT, "--exceptions", str(tmp_path)]) == 0
    assert f"ACCEPTED {GHSA}" in capsys.readouterr().out
    _record(tmp_path, expires_at=(date.today() + timedelta(days=91)).isoformat())
    assert main([str(report), "--project", PROJECT, "--exceptions", str(tmp_path)]) == 1
    assert "at most 90 days" in capsys.readouterr().err


def test_committed_exception_record_is_valid_and_scoped_to_the_desktop_audit():
    record = ROOT / "release" / "security-exceptions" / f"{GHSA}.md"
    accepted, issues = check(_report(_advisory(GHSA)), PROJECT, record.parent, TODAY)
    assert (accepted, issues) == ([GHSA], [])
    assert check(_report(_advisory(GHSA)), "apps/web-admin-next", record.parent, TODAY)[0] == []
    assert check_exception_records([str(record.parent)]) == 0
