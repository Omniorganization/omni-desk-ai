from __future__ import annotations

from pathlib import Path

from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.offline.connectivity import ConnectivityManager, ProbeResult, ReconnectSyncWorker


def _online_manager() -> ConnectivityManager:
    return ConnectivityManager(probes=[lambda: ProbeResult("dns", True, "ok"), lambda: ProbeResult("backend_health", True, "ok")])


def test_reconnect_worker_syncs_outbox_before_update_check(tmp_path: Path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    store.ensure_user("system")
    store.enqueue_local_operation(actor="system", operation_type="message.created", payload={"message_id": "m1"}, idempotency_key="m1")
    calls: list[str] = []

    def upload(items):
        calls.append("upload")
        assert len(items) == 1
        return {"ok": True, "remote_seq": 42}

    def update_check():
        calls.append("update")
        return {"ok": True, "status": "staged"}

    worker = ReconnectSyncWorker(connectivity=_online_manager(), store=store, upload=upload, update_check=update_check)
    result = worker.run_once()

    assert result["ok"] is True
    assert result["synced"] == 1
    assert calls == ["upload", "update"]
    assert store.sync_state(actor="system")["outbox"]["synced"] == 1


def test_reconnect_worker_does_not_update_when_upload_fails(tmp_path: Path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    store.ensure_user("system")
    store.enqueue_local_operation(actor="system", operation_type="message.created", payload={"message_id": "m1"}, idempotency_key="m1")
    calls: list[str] = []

    def upload(_items):
        calls.append("upload")
        raise RuntimeError("remote unavailable")

    def update_check():
        calls.append("update")
        return {"ok": True}

    worker = ReconnectSyncWorker(connectivity=_online_manager(), store=store, upload=upload, update_check=update_check)
    result = worker.run_once()

    assert result["ok"] is False
    assert result["state"] == "failed"
    assert calls == ["upload"]
    assert store.sync_state(actor="system")["outbox"]["pending"] == 1


def test_reconnect_worker_stays_local_only_when_probes_fail(tmp_path: Path):
    store = AppSyncStore(tmp_path / "app_sync.json")
    manager = ConnectivityManager(probes=[lambda: ProbeResult("dns", False, "no route")])
    worker = ReconnectSyncWorker(connectivity=manager, store=store, update_check=lambda: {"ok": True})

    result = worker.run_once()

    assert result["state"] == "local_only"
    assert result["update"] is None
    assert store.network_state["state"] == "local_only"
