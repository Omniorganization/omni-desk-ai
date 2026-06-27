from __future__ import annotations

import pytest

from omnidesk_agent.storage.sqlite import close_all_open_connections


@pytest.fixture(autouse=True)
def _isolate_optional_integration_route_env(monkeypatch):
    """Keep optional integration routes out of unrelated gateway tests.

    Tests that exercise BigSeller route registration can still set the variable
    explicitly inside the test. The default test process should mirror the
    production-safe gateway default: optional private-approval routes are absent
    unless explicitly enabled.
    """

    monkeypatch.delenv("BIGSELLER_REGISTER_ROUTES", raising=False)
    yield


@pytest.fixture(autouse=True)
def _close_sqlite_connections_after_test():
    yield
    close_all_open_connections()
