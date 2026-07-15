from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from omnidesk_agent.appsync.lease_safe_chat_repository import (
    ChatEventSequenceConflict,
    ChatLeaseLost,
    ChatRequestInProgress,
    ChatReservation,
    PostgresChatRepository,
)
from omnidesk_agent.appsync.migrated_postgres_store import (
    MigratedMultiInstancePostgresAppSyncStore,
)
from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations
from omnidesk_agent.models.base import ModelResponse


def _dsn() -> str:
    value = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    return value


def test_atomic_chat_is_single_writer_replayable_and_multi_instance_safe() -> None:
    dsn = _dsn()
    namespace = f"test_{uuid.uuid4().hex}"
    assert apply_appsync_migrations(dsn, namespace=namespace) == [1, 2, 3]
    assert apply_appsync_migrations(dsn, namespace=namespace) == []

    store_a = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=4)
    store_b = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=4)
    repo_a = PostgresChatRepository(store_a, lease_seconds=60)
    repo_b = PostgresChatRepository(store_b, lease_seconds=60)
    actor = f"operator-{uuid.uuid4().hex[:8]}"
    payload = {"content": "hello", "model_profile": "fast"}
    barrier = threading.Barrier(2)

    def reserve(repo: PostgresChatRepository) -> ChatReservation:
        barrier.wait(timeout=10)
        return repo.reserve(
            actor=actor,
            endpoint="conversations.ask",
            idempotency_key="atomic-key",
            payload=payload,
            conversation_id=None,
            title="Atomic test",
            source_device_id=None,
            content="hello",
            last_event_id=0,
        )

    outcomes: list[ChatReservation | BaseException] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve, repo) for repo in (repo_a, repo_b)]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=20))
            except BaseException as exc:
                outcomes.append(exc)

    winners = [item for item in outcomes if isinstance(item, ChatReservation)]
    conflicts = [item for item in outcomes if isinstance(item, ChatRequestInProgress)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    reservation = winners[0]
    owner_repo = repo_a if outcomes[0] is reservation else repo_b
    other_repo = repo_b if owner_repo is repo_a else repo_a

    owner_repo.append_event(
        reservation,
        sequence=1,
        event="chat.started",
        data={"conversation_id": reservation.conversation_id},
        status="running",
    )
    with pytest.raises(ChatLeaseLost):
        other_repo.append_event(
            replace(reservation, lease_owner="stale-worker"),
            sequence=2,
            event="chat.delta",
            data={"text": "must-not-persist"},
            status="running",
        )

    response = ModelResponse(
        text="world",
        provider="test",
        model="deterministic",
        profile="fast",
        usage={"output_tokens": 1},
    )
    owner_repo.complete(reservation, response, status="finalizing")
    owner_repo.append_event(
        reservation,
        sequence=2,
        event="chat.usage",
        data={"output_tokens": 1},
        status="finalizing",
    )
    owner_repo.append_event(
        reservation,
        sequence=3,
        event="chat.completed",
        data={"conversation_id": reservation.conversation_id},
        status="completed",
    )

    replay = other_repo.reserve(
        actor=actor,
        endpoint="conversations.ask",
        idempotency_key="atomic-key",
        payload=payload,
        conversation_id=None,
        title="Atomic test",
        source_device_id=None,
        content="hello",
        last_event_id=1,
    )
    assert replay.status == "completed"
    assert replay.response["assistant_message"]["content"] == "world"
    assert [event["sequence"] for event in replay.events] == [1, 2, 3]
    messages = other_repo.list_messages(actor, replay.conversation_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]

    created = store_a.create_conversation(
        actor=actor,
        title="Cross-instance visibility",
        source_device_id=None,
    )
    observed = store_b.get_conversation(created["conversation_id"], actor=actor)
    assert observed["title"] == "Cross-instance visibility"

    store_a.close()
    store_b.close()


def test_expired_lease_is_rejected_and_event_conflicts_fail_closed() -> None:
    dsn = _dsn()
    namespace = f"test_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    repo = PostgresChatRepository(store, lease_seconds=30)
    actor = f"operator-{uuid.uuid4().hex[:8]}"

    reservation = repo.reserve(
        actor=actor,
        endpoint="conversations.ask",
        idempotency_key="event-key",
        payload={"content": "hello"},
        conversation_id=None,
        title="Events",
        source_device_id=None,
        content="hello",
        last_event_id=0,
    )
    repo.append_event(
        reservation,
        sequence=1,
        event="chat.started",
        data={"conversation_id": reservation.conversation_id},
        status="running",
    )
    repo.append_event(
        reservation,
        sequence=1,
        event="chat.started",
        data={"conversation_id": reservation.conversation_id},
        status="running",
    )
    with pytest.raises(ChatEventSequenceConflict):
        repo.append_event(
            reservation,
            sequence=1,
            event="chat.delta",
            data={"text": "different"},
            status="running",
        )

    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE omnidesk_appsync_chat_requests "
            "SET lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())-1 "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s",
            (namespace, reservation.organization_id, actor, "conversations.ask", "event-key"),
        )
        conn.commit()
    with pytest.raises(ChatLeaseLost):
        repo.renew_lease(reservation)
    with pytest.raises(ChatLeaseLost):
        repo.fail(reservation, {"code": "must-not-write"})
    store.close()


def test_strict_repository_rejects_unprovisioned_actor() -> None:
    dsn = _dsn()
    namespace = f"test_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    repo = PostgresChatRepository(store, lease_seconds=30, allow_implicit_provisioning=False)
    actor = f"missing-{uuid.uuid4().hex[:8]}"
    with pytest.raises(PermissionError, match="identity_not_provisioned"):
        repo.organization_for_actor(actor)
    with pytest.raises(PermissionError, match="identity_not_provisioned"):
        repo.reserve(
            actor=actor,
            endpoint="conversations.ask",
            idempotency_key="strict-key",
            payload={"content": "hello"},
            conversation_id=None,
            title="Strict",
            source_device_id=None,
            content="hello",
            last_event_id=0,
        )
    store.close()
