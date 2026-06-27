from __future__ import annotations

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerSyncErrorQueue
from omnidesk_agent.integrations.bigseller.mapper import BigSellerSkuMapper
from omnidesk_agent.integrations.bigseller.mock_adapter import MockBigSellerAdapter
from omnidesk_agent.integrations.bigseller.worker import (
    BigSellerConnectorContext,
    BigSellerSyncWorker,
)


def test_order_sync_processes_once_then_skips_duplicates(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=True, audit_log_path=tmp_path / "bigseller_audit.jsonl"
    )
    context = BigSellerConnectorContext.from_config(config)

    first = context.worker.sync_orders()
    second = context.worker.sync_orders()

    assert first.total == 2
    assert first.processed == 2
    assert first.failed == 0
    assert second.total == 2
    assert second.skipped == 2
    audit_text = (tmp_path / "bigseller_audit.jsonl").read_text(encoding="utf-8")
    assert "sync.start" in audit_text
    assert "sync.end" in audit_text
    assert "access-token" not in audit_text


def test_order_sync_queues_failures_and_dead_letters_after_max_retries(tmp_path):
    class FailingMapper(BigSellerSkuMapper):
        def map_order(self, order):  # type: ignore[no-untyped-def]
            raise RuntimeError("access_token=raw-secret")

    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        max_retries=1,
        audit_log_path=tmp_path / "audit.jsonl",
    )
    error_queue = BigSellerSyncErrorQueue(max_retries=1)
    worker = BigSellerSyncWorker(
        config,
        MockBigSellerAdapter(config),
        mapper=FailingMapper(),
        errors=error_queue,
    )

    first = worker.sync_orders()
    second = worker.sync_orders()

    assert first.failed == 2
    assert second.failed == 2
    assert error_queue.stats()["dead_letter"] == 2
    assert "raw-secret" not in error_queue.list(status="dead_letter")[0].error_message
