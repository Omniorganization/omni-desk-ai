"""BigSeller connector scaffold for OmniDesk Enterprise Agent."""

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.mock_adapter import MockBigSellerAdapter
from omnidesk_agent.integrations.bigseller.worker import (
    BigSellerConnectorContext,
    BigSellerSyncWorker,
)

__all__ = [
    "BigSellerConfig",
    "BigSellerConnectorContext",
    "BigSellerSyncWorker",
    "MockBigSellerAdapter",
]
