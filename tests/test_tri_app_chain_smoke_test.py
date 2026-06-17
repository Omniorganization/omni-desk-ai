from __future__ import annotations

import pytest

from scripts.tri_app_chain_smoke_test import SmokeFailure, build_plan, execute_plan


class _Response:
    def __init__(self, body: bytes = b'{"ok": true, "correlation_id": "chain-1"}', status: int = 200) -> None:
        self._body = body
        self.status = status

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


def _set_complete_chain_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_BASE_URL", "https://admin.example.test")
    monkeypatch.setenv("OMNIDESK_DESKTOP_API_BASE_URL", "https://desktop.example.test/api")
    monkeypatch.setenv("OMNIDESK_MOBILE_API_BASE_URL", "https://mobile.example.test/api")
    monkeypatch.setenv("OMNIDESK_WEB_ADMIN_ADMIN_TOKEN", "web-secret")
    monkeypatch.setenv("OMNIDESK_DESKTOP_CLIENT_TOKEN", "desktop-secret")
    monkeypatch.setenv("OMNIDESK_MOBILE_CLIENT_TOKEN", "mobile-secret")
    monkeypatch.setenv("OMNIDESK_CHAIN_SESSION_PATH", "/smoke/session")
    monkeypatch.setenv("OMNIDESK_CHAIN_APPROVAL_PATH", "/smoke/approval")
    monkeypatch.setenv("OMNIDESK_CHAIN_AUDIT_PATH", "/smoke/audit")
    monkeypatch.setenv("OMNIDESK_CHAIN_NOTIFICATION_PATH", "/smoke/notification")


def test_build_plan_covers_three_clients_and_four_chain_steps(monkeypatch) -> None:
    _set_complete_chain_env(monkeypatch)

    plan = build_plan()

    assert len(plan) == 12
    assert {item.client for item in plan} == {"web-admin", "desktop", "mobile"}
    assert {item.step for item in plan} == {"session", "approval", "audit", "notification"}
    assert all("secret" not in item.url for item in plan)


def test_execute_plan_sends_authorization_without_printing_secret(monkeypatch) -> None:
    _set_complete_chain_env(monkeypatch)
    seen = []

    def opener(req, timeout):
        seen.append((req, timeout))
        return _Response()

    results = execute_plan(
        build_plan(),
        correlation_id="chain-1",
        expected_version="1.11.4",
        timeout=3,
        require_correlation_echo=True,
        opener=opener,
    )

    assert len(results) == 12
    assert seen[0][0].headers["Authorization"] == "Bearer web-secret"
    assert seen[0][1] == 3
    assert all("token" not in str(result).lower() for result in results)


def test_execute_plan_rejects_ok_false_response(monkeypatch) -> None:
    _set_complete_chain_env(monkeypatch)

    def opener(req, timeout):
        return _Response(body=b'{"ok": false, "correlation_id": "chain-1"}')

    with pytest.raises(SmokeFailure, match="ok=false"):
        execute_plan(
            build_plan(),
            correlation_id="chain-1",
            expected_version="1.11.4",
            timeout=3,
            require_correlation_echo=True,
            opener=opener,
        )


def test_execute_plan_rejects_missing_correlation_echo(monkeypatch) -> None:
    _set_complete_chain_env(monkeypatch)

    def opener(req, timeout):
        return _Response(body=b'{"ok": true}')

    with pytest.raises(SmokeFailure, match="did not echo correlation id"):
        execute_plan(
            build_plan(),
            correlation_id="chain-1",
            expected_version="1.11.4",
            timeout=3,
            require_correlation_echo=True,
            opener=opener,
        )
