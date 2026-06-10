from __future__ import annotations
from omnidesk_agent.core.run_store import RunStore


def test_run_store_lifecycle(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    rid = store.create({"channel": "local-cli", "sender_id": "owner", "text": "hello"})
    assert store.get(rid)["status"] == "planned"
    store.save_waiting(rid, {"goal": "g", "steps": [], "plan_id": "p", "rationale": "r"}, 0, [], "approval1")
    assert store.get(rid)["status"] == "waiting_approval"
    store.complete(rid, "completed", [{"ok": True}])
    assert store.get(rid)["status"] == "completed"
