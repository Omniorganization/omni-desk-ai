from __future__ import annotations

from datetime import date
from pathlib import Path

from scripts.check_security_exceptions import _validate_exception


def _record(path: Path, *, expires_at: str) -> Path:
    path.write_text(
        "\n".join(
            [
                "# Security Exception: GHSA-aaaa-bbbb-cccc",
                "",
                "Exception ID: GHSA-aaaa-bbbb-cccc",
                "Status: active",
                "Owner: security-owner",
                "Scope: test-only dependency path",
                f"Expires At: {expires_at}",
                "Upstream Tracking: upstream issue 123",
                "Compensating Controls: isolated runtime and blocking audit",
                "",
                "## Impact",
                "Test impact.",
                "## Runtime reachability",
                "Not reachable in production.",
                "## Compensating control",
                "Blocking audit remains enabled.",
                "## Removal criteria",
                "Remove after the upstream fix.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_security_exception_horizon_fails_inside_renewal_window(tmp_path: Path) -> None:
    path = _record(tmp_path / "GHSA-aaaa-bbbb-cccc.md", expires_at="2026-08-10")
    issues = _validate_exception(
        path,
        "GHSA-aaaa-bbbb-cccc",
        date(2026, 7, 17),
        fail_within_days=30,
    )
    assert any("expires in 24 day(s)" in issue for issue in issues)


def test_security_exception_horizon_allows_sufficient_runway(tmp_path: Path) -> None:
    path = _record(tmp_path / "GHSA-aaaa-bbbb-cccc.md", expires_at="2026-10-31")
    issues = _validate_exception(
        path,
        "GHSA-aaaa-bbbb-cccc",
        date(2026, 7, 17),
        fail_within_days=30,
    )
    assert issues == []


def test_security_exception_default_policy_only_fails_after_expiry(tmp_path: Path) -> None:
    path = _record(tmp_path / "GHSA-aaaa-bbbb-cccc.md", expires_at="2026-07-18")
    issues = _validate_exception(
        path,
        "GHSA-aaaa-bbbb-cccc",
        date(2026, 7, 17),
    )
    assert issues == []
