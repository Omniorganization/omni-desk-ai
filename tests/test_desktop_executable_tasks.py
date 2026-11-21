"""Desktop tasks are scoped, approval-bound actions, not free text.

Before this, a gateway task carried no capability or scope, every claimed task
fell to a dry-run executor and was reported "completed" although nothing ran,
and an approval authorized the first 120 characters of a chat message. Now an
executable task carries one validated action, its approval shows and binds that
exact action (by hash), only a runtime that advertises the capability can claim
it, and the result the runtime reports is visible to the task's creator only.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from omnidesk_agent.appsync.store import TASK_RESULT_MAX_BYTES, describe_task_action, normalize_task_action, task_action_hash
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

OWNER = "olivia-owner"
APPROVER = "andrea-approver"  # approvals need someone other than the requester
OPERATOR = "oscar-operator"
READ = {"capability": "workspace.read_file", "scope": {"workspace": "reports", "relative_path": "q3.md"}}


@pytest.fixture(params=["json", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "json":
        yield StrictJsonAppSyncStore(tmp_path / "appsync.json")
        return
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"executable_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    pg = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    try:
        yield pg
    finally:
        pg.close()


def test_action_validation_rejects_anything_beyond_its_scope() -> None:
    assert normalize_task_action(READ) == READ
    listing = normalize_task_action({"capability": "workspace.list_directory", "scope": {"relative_path": "."}})
    assert listing["scope"] == {"workspace": ".", "relative_path": "."}
    for bad in (
        {"capability": "shell.run", "scope": {"relative_path": "x"}},
        {**READ, "command": "rm -rf /"},
        {"capability": "workspace.read_file", "scope": {"relative_path": "x", "network": True}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "/etc/passwd"}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "../secret"}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "a/./b"}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "a\\b"}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "a\x00b"}},
        {"capability": "workspace.read_file", "scope": {"relative_path": "."}},
        {"capability": "workspace.read_file", "scope": {"workspace": "../..", "relative_path": "x"}},
        "read q3.md",
    ):
        with pytest.raises(ValueError):
            normalize_task_action(bad)


def test_action_hash_is_the_one_the_desktop_computes() -> None:
    # The same literal is pinned in apps/desktop-tauri/tests/executor.test.ts.
    assert task_action_hash(normalize_task_action(READ)) == "sha256:9374c2215e21e7adebd8cf20a327ad25707bd1723946b6f08c513646adb5078a"


def _executable_task(store: Any, *, action: dict[str, Any] = READ) -> dict[str, Any]:
    store.ensure_user(OWNER)
    store.ensure_user(OPERATOR)
    conversation = store.create_conversation(actor=OWNER, title="ops")
    return store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="please send me the Q3 report", action=action)


def test_the_approval_shows_and_binds_the_exact_action(store: Any) -> None:
    created = _executable_task(store)
    task, approval = created["task"], created["approval"]
    assert task["requires_desktop_runtime"] is True and task["status"] == "blocked"
    assert task["capability"] == "workspace.read_file" and task["action"] == READ
    assert task["action_hash"] == task_action_hash(READ) == approval["action_hash"]
    assert approval["action_detail"] == READ
    # The approver reads what will run, not the chat text that asked for it.
    assert approval["action"] == describe_task_action(READ)
    assert "reports/q3.md" in approval["action"] and "please send" not in approval["action"]


def test_only_a_runtime_that_advertises_the_capability_claims_it(store: Any) -> None:
    created = _executable_task(store)
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor=APPROVER, decision="approved")
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="desk", platform="mac")
    assert store.claim_next_task(actor=OWNER, device_id="dev_desk", capabilities=["chat", "local-runtime"]) is None
    claimed = store.claim_next_task(actor=OWNER, device_id="dev_desk", capabilities=["workspace.read_file"])
    assert claimed and claimed["task_id"] == created["task"]["task_id"]
    assert claimed["action"] == READ and claimed["action_hash"] == created["task"]["action_hash"]


def test_the_result_is_reported_by_the_lease_holder_and_seen_by_the_creator_only(store: Any) -> None:
    created = _executable_task(store)
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor=APPROVER, decision="approved")
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="desk", platform="mac")
    task_id = store.claim_next_task(actor=OWNER, device_id="dev_desk", capabilities=["workspace.read_file"])["task_id"]
    with pytest.raises(ValueError, match="at most"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="completed", device_id="dev_desk", result={"content": "x" * TASK_RESULT_MAX_BYTES})
    with pytest.raises(ValueError, match="completed or failed"):
        store.update_task_status(task_id=task_id, actor=OWNER, status="running", device_id="dev_desk", result={"content": "x"})
    done = store.update_task_status(task_id=task_id, actor=OWNER, status="completed", result_summary="workspace file read (17 characters)", device_id="dev_desk", result={"content": "quarterly numbers"})
    assert done["status"] == "completed" and done["result"] == {"content": "quarterly numbers"}
    assert store.get_task(task_id, actor=OWNER)["result"] == {"content": "quarterly numbers"}
    seen_by_other = store.get_task(task_id, actor=OPERATOR)
    assert "result" not in seen_by_other and seen_by_other["result_summary"] == "workspace file read (17 characters)"


def test_free_text_desktop_tasks_stay_claimable_but_are_not_actions(store: Any) -> None:
    store.ensure_user(OWNER)
    conversation = store.create_conversation(actor=OWNER, title="ops")
    created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="fix the build", requires_desktop_runtime=True)
    assert created["task"]["capability"] is None and created["task"]["action"] == {}
    store.decide_approval(approval_id=created["approval"]["approval_id"], actor=APPROVER, decision="approved")
    store.register_device(actor=OWNER, device_id="dev_desk", device_type="desktop", name="desk", platform="mac")
    claimed = store.claim_next_task(actor=OWNER, device_id="dev_desk", capabilities=["workspace.read_file"])
    # The desktop executor reports such a task as failed: nothing it could run was approved.
    assert claimed and claimed["action"] == {} and claimed["action_hash"] is None


# ------------------------------------------------------------------ routes ---


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


def test_routes_validate_actions_and_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", OPERATOR)
    owner = {"authorization": "Bearer owner-token", "x-omnidesk-actor": OWNER}
    # The operator requests and runs the action; the owner approves it (a
    # requester never approves their own request).
    operator = {"authorization": "Bearer operator-token", "x-omnidesk-actor": OPERATOR}
    with TestClient(create_app(_cfg(tmp_path))) as client:
        conversation = client.post("/app/conversations", headers={**operator, "idempotency-key": "c1"}, json={"title": "ops"}).json()["conversation"]
        path = f"/app/conversations/{conversation['conversation_id']}/messages"
        bad = client.post(path, headers={**operator, "idempotency-key": "m0"}, json={"content": "x", "action": {"capability": "shell.run", "scope": {"relative_path": "x"}}})
        assert bad.status_code == 422
        created = client.post(path, headers={**operator, "idempotency-key": "m1"}, json={"content": "send the report", "action": READ}).json()
        assert created["approval"]["action_detail"] == READ
        decided = client.post(f"/app/approvals/{created['approval']['approval_id']}/decide", headers={**owner, "idempotency-key": "d1"}, json={"decision": "approved"})
        assert decided.status_code == 200, decided.text
        client.post("/app/devices/register", headers={**operator, "idempotency-key": "r1"}, json={"device_id": "dev_desk_route", "device_type": "desktop", "name": "desk", "platform": "mac"})
        claimed = client.post("/app/runtime/desktop/claim", headers=operator, json={"device_id": "dev_desk_route", "capabilities": ["workspace.read_file"]}).json()["task"]
        assert claimed["action"] == READ
        status_path = f"/app/tasks/{claimed['task_id']}/status"
        too_big = client.post(status_path, headers={**operator, "idempotency-key": "s0"}, json={"status": "completed", "assigned_runtime_device_id": "dev_desk_route", "result": {"content": "x" * TASK_RESULT_MAX_BYTES}})
        assert too_big.status_code == 422
        done = client.post(status_path, headers={**operator, "idempotency-key": "s1"}, json={"status": "completed", "result_summary": "workspace file read", "assigned_runtime_device_id": "dev_desk_route", "result": {"content": "numbers"}})
        assert done.status_code == 200 and done.json()["task"]["result"] == {"content": "numbers"}
