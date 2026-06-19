from __future__ import annotations

import pytest

from omnidesk_agent.storage.sqlite import close_all_open_connections


@pytest.fixture(autouse=True)
def _close_sqlite_connections_after_test():
    yield
    close_all_open_connections()
