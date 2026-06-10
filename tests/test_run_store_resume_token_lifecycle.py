from __future__ import annotations
from omnidesk_agent.core.run_store import RunStore


def test_resume_token_only_exists_while_waiting(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    rid = store.create({"channel": "local", "sender_id": "u", "text": "x"})
    assert store.get(rid)["resume_token"] is None

    token1 = store.save_waiting(rid, {"goal": "g", "steps": [], "plan_id": "p"}, 0, [], "a1", {"tool": "computer"})
    assert token1
    assert store.get(rid)["resume_token"] == token1
    store.require_resume_token(rid, token1)

    token2 = store.save_waiting(rid, {"goal": "g", "steps": [], "plan_id": "p"}, 0, [], "a2", {"tool": "computer"})
    assert token2 != token1

    store.complete(rid, "completed", [])
    assert store.get(rid)["resume_token"] is None
