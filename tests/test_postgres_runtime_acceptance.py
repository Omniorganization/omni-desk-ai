from __future__ import annotations

import os
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnidesk_agent.channels.base import ChannelMessage
from omnidesk_agent.core.token_budget import TokenBudgetConfig
from omnidesk_agent.repositories.postgres import PostgresRepositoryFactory
from omnidesk_agent.repositories.postgres_pool import PostgresUnavailable, SharedPostgresConnectionPool


@pytest.fixture
def isolated_postgres():
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "runtime_acceptance_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_three_instances_two_workers_process_deduplicated_jobs_exactly_once(isolated_postgres):
    factories = [PostgresRepositoryFactory(isolated_postgres, pool_size=4) for _ in range(3)]
    try:
        queues = [factory.job_queue() for factory in factories]
        barrier = Barrier(3)

        def produce(queue):
            barrier.wait(timeout=5)
            return [queue.enqueue(ChannelMessage(
                channel="acceptance", sender_id="synthetic-operator", thread_id="isolated",
                message_id=f"message-{index}", text="synthetic job",
            )) for index in range(48)]

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(produce, queues))
        created_ids = [row["job_id"] for batch in results for row in batch if row["created"]]
        assert len(created_ids) == len(set(created_ids)) == 48
        assert len({row["job_id"] for batch in results for row in batch}) == 48
        workers_ready = Barrier(2)

        def consume(queue):
            workers_ready.wait(timeout=5)
            processed = []
            while (job := queue.claim_next()) is not None:
                queue.complete(job["id"], {"synthetic": True})
                processed.append(job["id"])
            return processed

        with ThreadPoolExecutor(max_workers=2) as executor:
            consumed = list(executor.map(consume, queues[:2]))
        processed_ids = [job for batch in consumed for job in batch]
        assert len(processed_ids) == len(set(processed_ids)) == 48
        assert set(processed_ids) == set(created_ids)
        for factory, queue in zip(factories, queues):
            assert factory.readiness_check()["ok"]
            assert queue.claim_next() is None
            assert all(queue.get(job_id)["status"] == "completed" for job_id in created_ids)
            assert factory.pool_stats()["in_use"] == factory.pool_stats()["waiters"] == 0
    finally:
        for factory in factories:
            factory.close()
    assert all(factory._pool.stats()["created"] == 0 for factory in factories)


def test_real_transaction_failure_rolls_back_and_reuses_connection(isolated_postgres):
    pool = SharedPostgresConnectionPool(isolated_postgres, max_size=2)
    try:
        with pytest.raises(ValueError, match="abort synthetic transaction"):
            with pool.connection() as connection:
                connection.execute("CREATE TABLE rollback_probe(id INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO rollback_probe VALUES (1)")
                raise ValueError("abort synthetic transaction")
        with pool.connection() as connection:
            assert connection.execute("SELECT to_regclass('rollback_probe')").fetchone() == (None,)
        assert pool.stats()["created"] == 1
        assert pool.ping()["ok"]
    finally:
        pool.close()
    assert pool.stats()["created"] == 0


def test_shared_call_budget_counts_all_rows_and_isolates_task_ids(isolated_postgres):
    factories = [PostgresRepositoryFactory(isolated_postgres) for _ in range(2)]
    try:
        first = factories[0].token_budget_manager(TokenBudgetConfig(per_task_max_llm_calls=1005))
        second = factories[1].token_budget_manager(TokenBudgetConfig(per_task_max_llm_calls=1005))
        task_id = "synthetic-task' quoted"
        with factories[0]._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO omnidesk_core_state VALUES ('llm_usage', %s, %s::jsonb, 1, 1)",
                    [(str(index), json.dumps({"task_id": task_id})) for index in range(1005)],
                )
        assert first.count_calls(task_id) == second.count_calls(task_id) == 1005
        assert first.count_calls("another-task") == 0
        assert not second.decide(model="synthetic", system="instructions", user="task", task_id=task_id, verified_required=True).allowed
        assert second.decide(model="synthetic", system="instructions", user="task", task_id="another-task", verified_required=True).allowed
    finally:
        for factory in factories:
            factory.close()


def test_real_memory_filters_ownership_before_candidate_limit(isolated_postgres):
    factory = PostgresRepositoryFactory(isolated_postgres)
    try:
        memory = factory.memory_store()
        own_id = memory.add_experience({"goal": "shipment", "task_type": "workflow", "success": True}, channel="chat", actor="alice")
        with factory._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO omnidesk_core_state VALUES ('structured_experiences', %s, %s::jsonb, 9999999999, 9999999999)",
                    [(f"foreign-{index}", json.dumps({"id": f"foreign-{index}", "goal": "shipment foreign", "channel": "chat", "actor": "bob", "memory_status": "candidate"})) for index in range(1005)],
                )
        rows = memory.retrieve_for_task("shipment", limit=1, channel="chat", actor="alice")
        assert [row["id"] for row in rows] == [own_id]
        assert memory.retrieve_for_task("shipment", channel="chat", actor="unknown") == []
        memory.update_memory_review(own_id, memory_status="blocked", confidence=0.2)
        assert memory.retrieve_for_task("shipment", channel="chat", actor="alice") == []
    finally:
        factory.close()


def test_real_disconnected_backend_is_discarded_and_next_transaction_recovers(isolated_postgres):
    import psycopg

    pool = SharedPostgresConnectionPool(isolated_postgres, max_size=2)
    try:
        with pytest.raises(psycopg.OperationalError):
            with pool.connection() as connection:
                pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                with psycopg.connect(isolated_postgres, autocommit=True) as controller:
                    assert controller.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchone() == (True,)
                connection.execute("SELECT 1")
        assert pool.stats()["created"] == pool.stats()["in_use"] == 0
        assert pool.ping()["ok"]
    finally:
        pool.close()


def test_real_factory_shutdown_is_terminal_and_releases_all_sessions(isolated_postgres):
    factory = PostgresRepositoryFactory(isolated_postgres)
    try:
        # Match the runtime bootstrap. Health probes must not create tables or
        # silently repair schema drift after startup.
        factory.transactional_outbox().init_schema()
        assert factory.health_check()["ok"]
        pool = factory._pool
        assert pool.stats()["created"] > 0
    finally:
        factory.close()
    factory.close()
    assert pool.stats()["created"] == pool.stats()["in_use"] == 0
    assert pool.stats()["closed"]
    with pytest.raises(PostgresUnavailable, match="closed"):
        factory.job_queue()
