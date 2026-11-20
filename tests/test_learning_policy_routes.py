"""The HTTP path to apply a learned artefact must need the owner role, record the
authenticated identity (not a request field) and require idempotency keys."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from omnidesk_agent.self_learning import cli
from omnidesk_agent.self_learning.routes import register_learning_policy_routes

ROLES = {"op-token": ("operator", "op@example.com"), "owner-token": ("owner", "owner@example.com"), "bot-token": ("owner", "agent")}
RANK = {"operator": 1, "owner": 2}


async def admin(request: Request, role: str):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token not in ROLES:
        raise HTTPException(status_code=401, detail="unauthenticated")
    have, actor = ROLES[token]
    if RANK[have] < RANK[role]:
        raise HTTPException(status_code=403, detail="role")
    return SimpleNamespace(actor=actor, role=have)


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    register_learning_policy_routes(app, SimpleNamespace(), admin, open_registry=lambda: cli.open_registry(tmp_path))
    with cli.open_registry(tmp_path) as registry:
        artifact = registry.register_candidate("workflow", "check-stock", "Check stock before quoting.")
        registry.record_evaluation(artifact.artifact_id, {"verdict": "improved", "method": "paired_replay_v1", "trace_count": 8})
    return TestClient(app), artifact.artifact_id


def _h(token, key=None):
    headers = {"authorization": f"Bearer {token}"}
    if key:
        headers["idempotency-key"] = key
    return headers


def test_approve_and_apply_over_http_records_the_authenticated_owner(client):
    http, artifact_id = client
    approval = http.post(f"/admin/learning/artifacts/{artifact_id}/request-approval", headers=_h("op-token", "k1")).json()

    assert http.post(f"/admin/learning/approvals/{approval['approval_id']}/decide", headers=_h("op-token", "k2"),
                     json={"decision": "approved"}).status_code == 403  # operators cannot decide
    decided = http.post(f"/admin/learning/approvals/{approval['approval_id']}/decide", headers=_h("owner-token", "k3"),
                        json={"decision": "approved", "reason": "replay improved"}).json()
    assert decided["status"] == "approved"

    policy = http.post(f"/admin/learning/artifacts/{artifact_id}/apply", headers=_h("owner-token", "k4"), json={}).json()
    assert policy["rules"] == ["Check stock before quoting."]
    history = http.get("/admin/learning/policy", headers=_h("op-token")).json()["history"]
    assert history[0]["actor"] == "owner@example.com"

    rolled = http.post("/admin/learning/policy/rollback", headers=_h("owner-token", "k5"), json={"reason": "drill"}).json()
    assert rolled["rules"] == []


def test_request_fields_cannot_choose_the_reviewer_and_agents_cannot_approve(client):
    http, artifact_id = client
    approval = http.post(f"/admin/learning/artifacts/{artifact_id}/request-approval", headers=_h("op-token", "k1")).json()
    forged = http.post(f"/admin/learning/approvals/{approval['approval_id']}/decide", headers=_h("owner-token", "k2"),
                       json={"decision": "approved", "reviewer": "someone-else"})
    assert forged.status_code == 422  # unknown fields are rejected
    bot = http.post(f"/admin/learning/approvals/{approval['approval_id']}/decide", headers=_h("bot-token", "k3"), json={"decision": "approved"})
    assert bot.status_code == 403


def test_side_effects_need_an_idempotency_key_and_errors_map_to_http(client):
    http, artifact_id = client
    assert http.post(f"/admin/learning/artifacts/{artifact_id}/apply", headers=_h("owner-token"), json={}).status_code == 428
    assert http.post(f"/admin/learning/artifacts/{artifact_id}/apply", headers=_h("owner-token", "k1"), json={}).status_code == 403  # not approved
    assert http.post("/admin/learning/artifacts/la_missing/request-approval", headers=_h("op-token", "k2")).status_code == 404
    assert http.get("/admin/learning/artifacts", headers={}).status_code == 401
