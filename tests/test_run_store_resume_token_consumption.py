from __future__ import annotations

import pytest

from omnidesk_agent.core.run_store import RunStore


def test_resume_token_consumed_on_resume(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run_id = store.create({"message": "x"})
    token = store.save_waiting(run_id, {"goal": "g", "steps": []}, 0, [], "approval-1", {"tool": "shell"})

    store.consume_resume_token(run_id, token)
    run = store.get(run_id)
    assert run["resume_token"] is None

    with pytest.raises(PermissionError):
        store.require_resume_token(run_id, token)
