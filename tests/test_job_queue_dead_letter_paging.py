"""Dead letters can be paged past the newest 200 and filtered by error class.

The self-healing probe needs every dead-lettered job, grouped by what failed,
not only the newest page of `list_dead_letters` (capped at 200).
"""
from __future__ import annotations

import pytest

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage


def _dead(queue: JobQueue, index: int, error: BaseException | str) -> str:
    job = queue.enqueue(ChannelMessage(channel="telegram", sender_id=f"u{index}", text=f"task {index}", message_id=f"m{index}"))
    queue.fail(job["job_id"], error)
    return job["job_id"]


def test_dead_letters_page_through_all_of_them_newest_first(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
    created = [_dead(queue, i, TimeoutError("provider timeout")) for i in range(230)]

    seen: list[str] = []
    cursor = None
    while True:
        page = queue.page_dead_letters(limit=100, cursor=cursor)
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 230 and set(seen) == set(created) and len(set(seen)) == 230
    assert len(queue.list_dead_letters(limit=500)) == 200  # the old listing stops at 200


def test_dead_letters_filter_and_count_by_error_class(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
    timeouts = {_dead(queue, i, TimeoutError("t")) for i in range(3)}
    denied = {_dead(queue, i + 10, PermissionError("p")) for i in range(2)}
    _dead(queue, 99, "plain text error")

    page = queue.page_dead_letters(error_class="TimeoutError")
    assert {item["id"] for item in page["items"]} == timeouts and page["next_cursor"] is None
    assert {item["id"] for item in queue.page_dead_letters(error_class="PermissionError")["items"]} == denied
    assert queue.dead_letter_counts() == {"TimeoutError": 3, "PermissionError": 2, "unknown": 1}


def test_a_page_stays_stable_while_new_dead_letters_arrive(tmp_path):
    queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
    for i in range(5):
        _dead(queue, i, RuntimeError("x"))
    first = queue.page_dead_letters(limit=2)
    _dead(queue, 50, RuntimeError("newer"))
    second = queue.page_dead_letters(limit=2, cursor=first["next_cursor"])
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    assert all(item["created_at"] <= first["items"][-1]["created_at"] for item in second["items"])


def test_a_bad_cursor_is_refused(tmp_path):
    with pytest.raises(ValueError, match="invalid dead-letter cursor"):
        JobQueue(tmp_path / "jobs.sqlite3").page_dead_letters(cursor="not-a-cursor")
