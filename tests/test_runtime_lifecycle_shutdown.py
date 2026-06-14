from __future__ import annotations

from pathlib import Path

from omnidesk_agent.memory.experience import ExperienceStore


def test_experience_store_close_is_idempotent(tmp_path: Path):
    store = ExperienceStore(tmp_path / "memory.sqlite3")
    store.add("task", plan="plan", outcome="ok")
    store.close()
    store.close()
    assert getattr(store, "_closed") is True


def test_experience_store_context_manager_closes(tmp_path: Path):
    with ExperienceStore(tmp_path / "memory.sqlite3") as store:
        store.add("task", plan="plan", outcome="ok")
    assert getattr(store, "_closed") is True
