"""Pre-auth quotas key on the real client; quota keys never come from client-controlled headers.

Regression for the shared web-admin bucket (security review 2026-10-21, M14) and
for the leftmost X-Forwarded-For / x-omnidesk-org quota keys.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from omnidesk_agent.config import ApiResourceGuardConfig, AppConfig
from omnidesk_agent.security.chat_resource_guard import ChatAwareApiResourceGuard
from omnidesk_agent.security.resource_guard import ApiResourceGuard, _client_key
from omnidesk_agent.server import create_app

WEB_ADMIN = ("127.0.0.1", 40000)
WEB_FIRST_GUARD = {"trusted_client_ip_header": "x-omnidesk-client-ip", "trusted_client_ip_header_peers": ["127.0.0.1"]}


def _request(path: str, *, peer=WEB_ADMIN, headers: dict[str, str] | None = None, method: str = "GET",
             raw: list[tuple[bytes, bytes]] | None = None) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    raw = (raw or []) + [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    scope = {"type": "http", "method": method, "scheme": "http", "server": ("127.0.0.1", 18789),
             "client": peer, "path": path, "headers": raw}
    return Request(scope, receive)


def _admitted(guard: ApiResourceGuard, path: str, client_ip: str | None) -> bool:
    headers = {"x-omnidesk-client-ip": client_ip} if client_ip else {}
    try:
        release = asyncio.run(guard.before_request(_request(path, headers=headers)))
    except HTTPException as exc:
        assert exc.status_code == 429
        return False
    release()
    return True


@pytest.mark.parametrize("guard_type", [ApiResourceGuard, ChatAwareApiResourceGuard])
def test_regression_gateway_limiter_poc_one_client_cannot_exhaust_shared_web_admin_budget(guard_type):
    # Same sequence as poc/clients/gateway_limiter_poc.py, with the web admin
    # forwarding each browser's address from the trusted loopback peer.
    guard = guard_type(ApiResourceGuardConfig(**WEB_FIRST_GUARD))
    attacker, admin = "203.0.113.66", "198.51.100.7"

    assert sum(_admitted(guard, "/admin/session/identity", attacker) for _ in range(120)) == 120
    assert not _admitted(guard, "/admin/session/identity", attacker), "attacker hits its own per-path cap"
    assert _admitted(guard, "/admin/session/identity", admin), "admin session check still admitted"

    assert sum(_admitted(guard, "/app/bootstrap", attacker) for _ in range(180)) == 120
    assert not _admitted(guard, "/app/projects", attacker), "attacker hits its own per-IP cap"
    assert _admitted(guard, "/app/projects", admin), "admin API call still admitted"


def test_without_trusted_proxy_config_the_web_admin_bucket_stays_shared():
    # Negative control: the fix depends on the deployment trusting the web
    # admin's loopback address (scripts/web_first_host.py writes it).
    guard = ChatAwareApiResourceGuard(ApiResourceGuardConfig())
    for _ in range(120):
        _admitted(guard, "/admin/session/identity", "203.0.113.66")
    assert not _admitted(guard, "/admin/session/identity", "198.51.100.7")


def test_client_ip_header_is_honored_only_from_its_configured_peers():
    header = {"x-omnidesk-client-ip": "198.51.100.9"}
    web_first = ApiResourceGuardConfig(**WEB_FIRST_GUARD)

    assert _client_key(_request("/x", headers=header), web_first) == "198.51.100.9"
    assert _client_key(_request("/x", peer=("203.0.113.5", 1), headers=header), web_first) == "203.0.113.5"
    # Trusting a peer for X-Forwarded-For (e.g. an ingress that passes headers
    # through) does not make it a source of the dedicated header.
    xff_only = ApiResourceGuardConfig(trusted_proxy_ips=["127.0.0.1"], trusted_client_ip_header="x-omnidesk-client-ip")
    assert _client_key(_request("/x", headers=header), xff_only) == "127.0.0.1"
    for bad in ("not-an-ip", "198.51.100.9, 203.0.113.1", ""):
        assert _client_key(_request("/x", headers={"x-omnidesk-client-ip": bad}), web_first) == "127.0.0.1"
    twice = [(b"x-omnidesk-client-ip", b"198.51.100.9"), (b"x-omnidesk-client-ip", b"198.51.100.10")]
    assert _client_key(_request("/x", raw=twice), web_first) == "127.0.0.1"
    assert _client_key(_request("/x", headers={"x-omnidesk-client-ip": "::ffff:198.51.100.9"}), web_first) == "198.51.100.9"
    assert _client_key(_request("/x", peer=("::ffff:127.0.0.1", 1), headers=header), web_first) == "127.0.0.1"


def test_ipv6_clients_are_keyed_per_64_at_the_gateway():
    web_first = ApiResourceGuardConfig(**WEB_FIRST_GUARD)
    first = _client_key(_request("/x", headers={"x-omnidesk-client-ip": "2001:DB8:1:2::aaaa"}), web_first)
    assert first == "2001:db8:1:2::/64"
    assert _client_key(_request("/x", peer=("2001:db8:1:2::bbbb", 1)), ApiResourceGuardConfig()) == first
    assert _client_key(_request("/x", peer=("2001:db8:1:3::1", 1)), ApiResourceGuardConfig()) != first


@pytest.mark.parametrize(
    ("forwarded", "trusted", "expected"),
    [
        (["198.51.100.77"], ["127.0.0.1"], "198.51.100.77"),
        (["6.6.6.6, 198.51.100.77"], ["127.0.0.1"], "198.51.100.77"),
        (["6.6.6.6", "198.51.100.77"], ["127.0.0.1"], "198.51.100.77"),
        (["6.6.6.6, 198.51.100.77, 10.0.0.2"], ["127.0.0.1", "10.0.0.0/8"], "198.51.100.77"),
        (["10.0.0.3, 10.0.0.2"], ["127.0.0.1", "10.0.0.0/8"], "10.0.0.3"),
        (["198.51.100.77, garbage"], ["127.0.0.1"], "127.0.0.1"),
        (["garbage, 198.51.100.77"], ["127.0.0.1"], "198.51.100.77"),
        ([], ["127.0.0.1"], "127.0.0.1"),
    ],
)
def test_forwarded_for_is_read_right_to_left_skipping_trusted_proxies(forwarded, trusted, expected):
    raw = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    request = Request({"type": "http", "method": "GET", "scheme": "http", "server": ("127.0.0.1", 18789),
                       "client": WEB_ADMIN, "path": "/x", "headers": raw})
    assert _client_key(request, ApiResourceGuardConfig(trusted_proxy_ips=trusted)) == expected


def test_client_ip_header_config_is_validated():
    assert ApiResourceGuardConfig().trusted_client_ip_header == ""
    assert ApiResourceGuardConfig().trusted_client_ip_header_peers == []
    for bad in ("X-OmniDesk-Client-IP", "x omnidesk", "x-" + "a" * 64, "x-forwarded-for", "forwarded"):
        with pytest.raises(ValidationError):
            ApiResourceGuardConfig(trusted_client_ip_header=bad)
    for peers in (["0.0.0.0/0"], ["::/0"], ["not-an-ip"], ["127.0.0.1", "*"]):
        with pytest.raises(ValidationError):
            ApiResourceGuardConfig(trusted_client_ip_header_peers=peers)
    assert ApiResourceGuardConfig(trusted_client_ip_header_peers=["127.0.0.1", "10.0.0.0/8"]).trusted_client_ip_header_peers


@pytest.mark.parametrize("guard_type", [ApiResourceGuard, ChatAwareApiResourceGuard])
def test_organization_quota_ignores_client_headers(guard_type):
    guard = guard_type(ApiResourceGuardConfig(max_requests_per_org_endpoint=2))
    for org in ("rotating-1", "rotating-2"):
        guard.check_authenticated(_request("/app/projects", headers={"x-omnidesk-org": org}), actor="alice", role="operator",
                                  organization="org_a")
    with pytest.raises(HTTPException) as limited:
        guard.check_authenticated(_request("/app/projects", headers={"x-omnidesk-org": "rotating-3"}), actor="alice",
                                  role="operator", organization="org_a")
    assert limited.value.detail == "organization rate limit exceeded"
    # Another organization's budget is untouched, whatever header alice sends.
    guard.check_authenticated(_request("/app/projects", headers={"x-omnidesk-org": "org_b"}), actor="bob", role="operator",
                              organization="org_b")


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    cfg.plugins.enabled = False
    return cfg


@pytest.mark.parametrize("trusted", [True, False])
def test_gateway_identity_checks_are_limited_per_forwarded_client(tmp_path, monkeypatch, trusted):
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "test-fixture-viewer-token")
    cfg = _cfg(tmp_path)
    cfg.api_resource_guard.max_requests_per_endpoint = 3
    if trusted:
        # TestClient's peer stands in for the web admin (assignment skips the IP validator).
        cfg.api_resource_guard.trusted_client_ip_header_peers = ["testclient"]
        cfg.api_resource_guard.trusted_client_ip_header = "x-omnidesk-client-ip"
    with TestClient(create_app(cfg)) as client:
        junk = {"authorization": "Bearer junk", "x-omnidesk-client-ip": "203.0.113.66"}
        assert [client.get("/admin/session/identity", headers=junk).status_code for _ in range(3)] == [403] * 3
        assert client.get("/admin/session/identity", headers=junk).status_code == 429
        legit = client.get("/admin/session/identity", headers={"authorization": "Bearer test-fixture-viewer-token",
                                                                "x-omnidesk-client-ip": "198.51.100.7"})
    assert legit.status_code == (200 if trusted else 429)


def test_gateway_uses_server_side_organization_for_quota_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "test-fixture-operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "test-fixture-viewer-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_ACTOR", "bob")
    cfg = _cfg(tmp_path)
    cfg.api_resource_guard.max_requests_per_org_endpoint = 2
    app = create_app(cfg)
    app.state.runtime.app_sync = SimpleNamespace(_organization_for_actor=lambda actor: {"alice": "org_a"}.get(actor, "org_b"))
    alice = {"authorization": "Bearer test-fixture-operator-token"}
    with TestClient(app) as client:
        statuses = [client.get("/admin/session/identity", headers={**alice, "x-omnidesk-org": f"spoof-{i}"}).status_code
                    for i in range(3)]
        other_org = client.get("/admin/session/identity", headers={"authorization": "Bearer test-fixture-viewer-token",
                                                                    "x-omnidesk-org": "org_a"})
    assert statuses == [200, 200, 429]
    assert other_org.status_code == 200
