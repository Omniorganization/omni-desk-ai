"""The PostgreSQL job queue pages dead letters and groups them by error class.

The PostgreSQL twin of agent-core's #261 (SQLite ``JobQueue``). autonomy's
self-healing probe needs every dead-lettered job, grouped by what failed, on
PostgreSQL deployments too. ``list_dead_letters`` returns only the newest page.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterator

import pytest

from omnidesk_agent.channels.base import ChannelMessage
from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory


@pytest.fixture
def queue() -> Iterator[Any]:
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "dead_letters_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    factory = PostgresRepositoryFactory(make_conninfo(dsn, options=f"-csearch_path={schema}"), pool_size=2)
    try:
        job_queue = factory.job_queue()
        job_queue.max_retries = 0  # the first failure dead-letters a job
        yield job_queue
    finally:
        factory.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _dead(queue: Any, index: int, error: BaseException | str) -> str:
    job = queue.enqueue(ChannelMessage(channel="telegram", sender_id=f"u{index}", text=f"task {index}", message_id=f"m{index}"))
    assert queue.fail(job["job_id"], error)["status"] == "dead_letter"
    return str(job["job_id"])


def test_dead_letters_page_through_all_of_them_newest_first(queue: Any) -> None:
    created = [_dead(queue, i, TimeoutError("provider timeout")) for i in range(120)]
    seen: list[str] = []
    stamps: list[float] = []
    cursor = None
    while True:
        page = queue.page_dead_letters(limit=50, cursor=cursor)
        seen.extend(item["id"] for item in page["items"])
        stamps.extend(float(item["created_at"]) for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 120 and set(seen) == set(created)
    assert stamps == sorted(stamps, reverse=True)


def test_dead_letters_filter_and_count_by_error_class(queue: Any) -> None:
    timeouts = {_dead(queue, i, TimeoutError("t")) for i in range(3)}
    denied = {_dead(queue, i + 10, PermissionError("p")) for i in range(2)}
    _dead(queue, 99, "plain text error")
    page = queue.page_dead_letters(error_class="TimeoutError")
    assert {item["id"] for item in page["items"]} == timeouts and page["next_cursor"] is None
    assert all(item["error_class"] == "TimeoutError" for item in page["items"])
    assert {item["id"] for item in queue.page_dead_letters(error_class="PermissionError")["items"]} == denied
    assert queue.dead_letter_counts() == {"TimeoutError": 3, "PermissionError": 2, "unknown": 1}


def test_a_page_stays_stable_while_new_dead_letters_arrive(queue: Any) -> None:
    for i in range(5):
        _dead(queue, i, RuntimeError("x"))
    first = queue.page_dead_letters(limit=2)
    _dead(queue, 50, RuntimeError("newer"))
    second = queue.page_dead_letters(limit=2, cursor=first["next_cursor"])
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    assert all(float(item["created_at"]) <= float(first["items"][-1]["created_at"]) for item in second["items"])


def test_a_bad_cursor_is_refused(queue: Any) -> None:
    with pytest.raises(ValueError, match="invalid dead-letter cursor"):
        queue.page_dead_letters(cursor="not-a-cursor")


def test_a_broken_connection_never_goes_back_into_the_pool() -> None:
    # psycopg sets `broken` after a lost server; reusing it fails the next caller.
    from omnidesk_agent.repositories.postgres_pool import SharedPostgresConnectionPool

    class Connection:
        closed = False
        broken = True

    assert SharedPostgresConnectionPool._usable(Connection()) is False
    Connection.broken = False
    assert SharedPostgresConnectionPool._usable(Connection()) is True
