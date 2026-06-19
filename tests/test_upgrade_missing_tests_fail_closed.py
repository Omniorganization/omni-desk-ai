from __future__ import annotations

import asyncio
from omnidesk_agent.self_upgrade.testing.regression_runner import RegressionRunner
from omnidesk_agent.self_upgrade.testing.security_test_runner import SecurityTestRunner


def test_regression_runner_missing_required_tests_fails_closed(tmp_path):
    result = asyncio.run(RegressionRunner(tmp_path).run())
    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["missing"]


def test_security_runner_missing_required_tests_fails_closed(tmp_path):
    result = asyncio.run(SecurityTestRunner(tmp_path).run())
    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["missing"]
