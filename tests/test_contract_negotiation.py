"""Clients and the gateway agree on a contract version before anything runs.

Versioning used to be a lockstep string comparison in a release script. An older
mobile build in the field talking to a newer gateway, or the other way round,
failed somewhere at run time with no compatibility signal at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.contract import CONTRACT_HEADER, CONTRACT_VERSION, ContractVersionMiddleware, contract_major
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

AUTH = {"authorization": "Bearer operator-token", "x-omnidesk-actor": "desk-operator"}


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
    return cfg


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "desk-operator")
    with TestClient(create_app(_cfg(tmp_path))) as test_client:
        yield test_client


def _other(major_delta: int, minor: int = 0) -> str:
    return f"{contract_major(CONTRACT_VERSION) + major_delta}.{minor}"  # type: ignore[operator]


def test_every_answer_names_the_gateways_contract(client: TestClient) -> None:
    response = client.get("/app/bootstrap", headers=AUTH)
    assert response.status_code == 200 and response.headers[CONTRACT_HEADER] == CONTRACT_VERSION
    # Errors too: a client can tell an incompatible gateway from a broken request.
    assert client.get("/app/bootstrap").headers[CONTRACT_HEADER] == CONTRACT_VERSION


def test_a_client_of_the_same_major_is_served_whatever_its_minor(client: TestClient) -> None:
    for version in (CONTRACT_VERSION, _other(0, 99)):
        assert client.get("/app/bootstrap", headers={**AUTH, CONTRACT_HEADER: version}).status_code == 200


def test_a_client_built_before_negotiation_is_served(client: TestClient) -> None:
    assert client.get("/app/bootstrap", headers=AUTH).status_code == 200


@pytest.mark.parametrize("delta", [1, -1])
def test_a_client_of_another_major_is_refused_before_anything_runs(client: TestClient, delta: int) -> None:
    version = _other(delta)
    response = client.post("/app/conversations", headers={**AUTH, CONTRACT_HEADER: version, "idempotency-key": "k1"}, json={"title": "t"})
    assert response.status_code == 426
    assert response.json() == {"ok": False, "code": "contract_version_unsupported", "error": "contract_version_unsupported", "gateway_contract": CONTRACT_VERSION, "client_contract": version}
    # Nothing was created.
    assert client.get("/app/bootstrap", headers=AUTH).status_code == 200
    assert client.get("/app/conversations", headers=AUTH).json()["conversations"] == []


def test_a_malformed_version_is_a_bad_request(client: TestClient) -> None:
    response = client.get("/app/bootstrap", headers={**AUTH, CONTRACT_HEADER: "latest"})
    assert response.status_code == 400 and response.json()["code"] == "contract_version_invalid"


def test_routes_outside_the_contract_are_untouched(client: TestClient) -> None:
    response = client.get("/health", headers={CONTRACT_HEADER: _other(5)})
    assert response.status_code != 426 and CONTRACT_HEADER not in response.headers


def test_the_desktop_webview_may_send_and_read_the_version(client: TestClient) -> None:
    preflight = client.options("/app/bootstrap", headers={"origin": "tauri://localhost", "access-control-request-method": "GET", "access-control-request-headers": f"authorization,{CONTRACT_HEADER}"})
    assert CONTRACT_HEADER in preflight.headers["access-control-allow-headers"].lower()
    answer = client.get("/app/bootstrap", headers={**AUTH, "origin": "tauri://localhost", CONTRACT_HEADER: CONTRACT_VERSION})
    assert CONTRACT_HEADER in answer.headers["access-control-expose-headers"].lower()
    refused = client.get("/app/bootstrap", headers={**AUTH, "origin": "tauri://localhost", CONTRACT_HEADER: _other(1)})
    # The webview can read why it was refused.
    assert refused.status_code == 426 and refused.headers["access-control-allow-origin"] == "tauri://localhost"


def test_the_gateway_refuses_to_start_with_a_malformed_version() -> None:
    with pytest.raises(ValueError):
        ContractVersionMiddleware(app=lambda *_: None, version="1")  # type: ignore[arg-type]
