from __future__ import annotations

import pytest

from omnidesk_agent.appsync.store import AppSyncStore
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
def _inject_local_appsync_for_production_route_policy_tests(
    request, monkeypatch, tmp_path
):
    """Keep route-policy tests independent from a live PostgreSQL service.

    The production backend fail-closed contract is tested separately. This
    module exercises request validation after application construction, so it
    injects an explicit local test double instead of weakening the factory.
    """

    module = getattr(getattr(request, "module", None), "__name__", "")
    if module.endswith("test_ga_1_04_production_hardening"):
        store = AppSyncStore(tmp_path / "route-policy-appsync.json")

        def create_test_store(_cfg):
            return store

        for target in (
            "omnidesk_agent.appsync.factory.create_appsync_store",
            "omnidesk_agent.appsync.routes.create_appsync_store",
            "omnidesk_agent.appsync.chat_routes.create_appsync_store",
        ):
            monkeypatch.setattr(target, create_test_store)
    yield


@pytest.fixture(autouse=True)
def _close_sqlite_connections_after_test():
    yield
    close_all_open_connections()
