from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one replacement target, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n\n" + content.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    chat_repository = Path("omnidesk_agent/appsync/chat_repository.py")
    replace_once(
        chat_repository,
        '            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope),))\n            cur.execute(\n                "SELECT organization_id FROM omnidesk_appsync_users WHERE namespace=%s AND user_id=%s",',
        '            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope),))\n            cur.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())")\n            clock_row = cur.fetchone()\n            if not clock_row:\n                raise RuntimeError("PostgreSQL clock is unavailable")\n            lease_now = float(clock_row[0])\n            cur.execute(\n                "SELECT organization_id FROM omnidesk_appsync_users WHERE namespace=%s AND user_id=%s",',
    )
    replace_once(
        chat_repository,
        "                if status in ACTIVE and float(existing[5] or 0) > now:\n",
        "                if status in ACTIVE and float(existing[5] or 0) > lease_now:\n",
    )
    replace_once(
        chat_repository,
        "                    now + self.lease_seconds,\n                    now,\n                    now,\n",
        "                    lease_now + self.lease_seconds,\n                    now,\n                    now,\n",
    )

    service = Path("omnidesk_agent/appsync/industrial_chat_service.py")
    replace_once(service, "from contextlib import suppress\n", "")
    replace_once(
        service,
        '''            except ChatLeaseLost:\n                logger.warning(\n                    "chat lease heartbeat stopped after lease loss",\n                    extra={"conversation_id": reservation.conversation_id},\n                )\n                return\n''',
        '''            except ChatLeaseLost:\n                logger.warning(\n                    "chat lease heartbeat stopped after lease loss",\n                    extra={"conversation_id": reservation.conversation_id},\n                )\n                return\n            except asyncio.CancelledError:\n                raise\n            except Exception:\n                logger.exception(\n                    "chat lease heartbeat renewal failed; retrying",\n                    extra={"conversation_id": reservation.conversation_id},\n                )\n''',
    )
    replace_once(
        service,
        '''        task.cancel()\n        with suppress(asyncio.CancelledError):\n            await task\n''',
        '''        task.cancel()\n        try:\n            await task\n        except asyncio.CancelledError:\n            return\n        except Exception:\n            logger.exception("chat lease heartbeat task failed during cleanup")\n''',
    )

    postgres_tests = Path("tests/test_postgres_appsync_atomic_chat.py")
    append_once(
        postgres_tests,
        "def test_reservation_uses_postgres_clock_for_lease_fencing",
        '''def test_reservation_uses_postgres_clock_for_lease_fencing(monkeypatch: pytest.MonkeyPatch) -> None:\n    dsn = _dsn()\n    namespace = f"test_{uuid.uuid4().hex}"\n    apply_appsync_migrations(dsn, namespace=namespace)\n    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)\n    repo = PostgresChatRepository(store, lease_seconds=30)\n    actor = f"operator-{uuid.uuid4().hex[:8]}"\n    payload = {"content": "clock-safe"}\n    repo.reserve(\n        actor=actor,\n        endpoint="conversations.ask",\n        idempotency_key="clock-key",\n        payload=payload,\n        conversation_id=None,\n        title="Clock",\n        source_device_id=None,\n        content="clock-safe",\n        last_event_id=0,\n    )\n    monkeypatch.setattr(\n        "omnidesk_agent.appsync.chat_repository.time.time",\n        lambda: 9_999_999_999.0,\n    )\n    with pytest.raises(ChatRequestInProgress):\n        repo.reserve(\n            actor=actor,\n            endpoint="conversations.ask",\n            idempotency_key="clock-key",\n            payload=payload,\n            conversation_id=None,\n            title="Clock",\n            source_device_id=None,\n            content="clock-safe",\n            last_event_id=0,\n        )\n    store.close()''',
    )

    heartbeat_tests = Path("tests/test_industrial_chat_heartbeat.py")
    heartbeat_tests.write_text(
        '''from __future__ import annotations\n\nimport asyncio\nfrom typing import Any\n\nimport pytest\n\nfrom omnidesk_agent.appsync.industrial_chat_service import IndustrialChatTurnService\nfrom omnidesk_agent.appsync.lease_safe_chat_repository import ChatLeaseLost, ChatReservation\n\n\ndef _reservation() -> ChatReservation:\n    return ChatReservation(\n        namespace="test",\n        organization_id="org",\n        actor="operator",\n        endpoint="conversations.ask",\n        idempotency_key="heartbeat-key",\n        payload_hash="hash",\n        conversation_id="conv",\n        user_message={"message_id": "msg"},\n        status="running",\n        lease_owner="worker",\n        response={},\n        events=(),\n    )\n\n\n@pytest.mark.asyncio\nasync def test_heartbeat_retries_transient_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:\n    class Repository:\n        lease_seconds = 30\n\n        def __init__(self) -> None:\n            self.calls = 0\n\n        def renew_lease(self, reservation: ChatReservation) -> None:\n            del reservation\n            self.calls += 1\n            if self.calls == 1:\n                raise OSError("temporary database outage")\n            raise ChatLeaseLost("lease was reclaimed")\n\n    repository = Repository()\n    service = object.__new__(IndustrialChatTurnService)\n    service.atomic_repository = repository\n\n    async def no_wait(_: float) -> None:\n        return None\n\n    monkeypatch.setattr(asyncio, "sleep", no_wait)\n    await service._lease_heartbeat(_reservation())\n    assert repository.calls == 2\n\n\n@pytest.mark.asyncio\nasync def test_cancel_heartbeat_does_not_mask_success_with_task_failure() -> None:\n    service = object.__new__(IndustrialChatTurnService)\n\n    async def failed() -> None:\n        raise OSError("heartbeat failed before cleanup")\n\n    task: asyncio.Task[Any] = asyncio.create_task(failed())\n    await asyncio.sleep(0)\n    await service._cancel_heartbeat(task)\n''',
        encoding="utf-8",
    )

    release_tests = Path("tests/test_release_governance_assets.py")
    append_once(
        release_tests,
        "def test_production_promotion_uses_complete_real_ga_gate",
        '''def test_production_promotion_uses_complete_real_ga_gate() -> None:\n    workflow = Path(".github/workflows/promote-production.yml").read_text(encoding="utf-8")\n    assert "check_real_ga_complete.py . --evidence-dir dist/external-evidence" in workflow\n    assert "check_external_ga_evidence.py . --evidence-dir dist/external-evidence" not in workflow''',
    )


if __name__ == "__main__":
    main()
