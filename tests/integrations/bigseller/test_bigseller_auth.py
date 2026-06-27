from __future__ import annotations

from datetime import timedelta

import pytest

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerConfigurationError
from omnidesk_agent.integrations.bigseller.mock_adapter import MockBigSellerAdapter
from omnidesk_agent.integrations.bigseller.schemas import utc_now
from omnidesk_agent.integrations.bigseller.worker import BigSellerConnectorContext


def test_expired_token_refreshes_before_request(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        access_token="old-token",
        refresh_token="refresh-token",
        token_expires_at=utc_now() - timedelta(seconds=5),
        audit_log_path=tmp_path / "audit.jsonl",
    )
    adapter = MockBigSellerAdapter(config)

    response = adapter.request("GET", "/mock/orders")

    assert len(response) == 2
    assert adapter.refresh_count == 1
    assert (
        adapter.token_manager.token_state.access_token
        == "mock-access-token-refreshed-1"
    )


def test_unauthorized_response_refreshes_once_and_retries(tmp_path):
    config = BigSellerConfig(
        enabled=True,
        use_mock=True,
        access_token="valid-looking-token",
        refresh_token="refresh-token",
        token_expires_at=utc_now() + timedelta(hours=1),
        audit_log_path=tmp_path / "audit.jsonl",
    )
    adapter = MockBigSellerAdapter(config, unauthorized_once=True)

    response = adapter.request("GET", "/mock/orders")

    assert len(response) == 2
    assert adapter.refresh_count == 1
    assert (
        adapter.token_manager.token_state.access_token
        == "mock-access-token-refreshed-1"
    )


def test_real_mode_missing_credentials_fails_closed(tmp_path):
    config = BigSellerConfig(
        enabled=True, use_mock=False, audit_log_path=tmp_path / "audit.jsonl"
    )
    context = BigSellerConnectorContext.from_config(config)

    with pytest.raises(BigSellerConfigurationError) as excinfo:
        context.worker.sync_orders()

    assert "BIGSELLER_BASE_URL" in str(excinfo.value)
    assert context.config.health()["ready"] is False
