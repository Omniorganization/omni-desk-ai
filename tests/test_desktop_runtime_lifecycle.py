from __future__ import annotations

from pathlib import Path

import pytest

from omnidesk_agent.appsync.desktop_runtime_routes import (
    _renew_in_memory,
    _task_control,
)
from omnidesk_agent.appsync.store import AppSyncStore


def _claimed_store(tmp_path: Path):
    store = AppSyncStore(tmp_path / "appsync.json")
    store.register_device(
        actor="alice",
        device_id="desktop-1",
        device_type="desktop",
        name="Desktop 1",
        platform="test",
        capabilities=["file_operation"],
    )
    store.register_device(
        actor="alice",
        device_id="desktop-2",
        device_type="desktop",
        name="Desktop 2",
        platform="test",
        capabilities=["file_operation"],
    )
    conversation = store.create_conversation(actor="alice", title="runtime")
    created = store.add_message_and_task(
        actor="alice",
        conversation_id=conversation["conversation_id"],
        content="perform bounded workspace operation",
        requires_desktop_runtime=True,
        risk="low",
    )
    task = store.claim_next_task(
        actor="alice",
        device_id="desktop-1",
        lease_seconds=60,
        capabilities=["file_operation"],
    )
    assert task is not None
    return store, created["task"]["task_id"], task


def test_in_memory_lease_renewal_is_owner_bound(tmp_path: Path) -> None:
    store, task_id, claimed = _claimed_store(tmp_path)
    renewed = _renew_in_memory(
        store,
        actor="alice",
        task_id=task_id,
        device_id="desktop-1",
        lease_seconds=120,
    )
    assert renewed["lease_expires_at"] > claimed["lease_expires_at"]
    with pytest.raises(PermissionError):
        _renew_in_memory(
            store,
            actor="alice",
            task_id=task_id,
            device_id="desktop-2",
            lease_seconds=120,
        )


def test_task_control_surfaces_cancel_and_lease_state(tmp_path: Path) -> None:
    store, task_id, _claimed = _claimed_store(tmp_path)
    running = _task_control(
        store,
        actor="alice",
        task_id=task_id,
        device_id="desktop-1",
    )
    assert running["status"] == "running"
    assert running["cancel_requested"] is False
    store.update_task_status(
        task_id=task_id,
        actor="alice",
        status="cancelled",
        assigned_runtime_device_id="desktop-1",
    )
    cancelled = _task_control(
        store,
        actor="alice",
        task_id=task_id,
        device_id="desktop-1",
    )
    assert cancelled["cancel_requested"] is True


def test_desktop_source_contains_durable_recovery_and_atomic_workspace_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    worker = (root / "apps/desktop-tauri/src/runtimeWorker.ts").read_text(
        encoding="utf-8"
    )
    native = (
        root / "apps/desktop-tauri/src-tauri/src/main.rs"
    ).read_text(encoding="utf-8")
    executor = (
        root / "apps/desktop-tauri/src/executor.ts"
    ).read_text(encoding="utf-8")
    assert "renewTaskLease" in worker
    assert "taskControl" in worker
    assert "AbortController" in worker
    assert "localStorage" in worker
    assert "flushStatusOutbox" in worker
    assert "expected_sha256" in native
    assert "atomic_write" in native
    assert "patch_workspace_file" in native
    assert "diff_workspace_file" in native
    assert "file_operation" in executor
