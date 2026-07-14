from __future__ import annotations

from pathlib import Path

import pytest

from omnidesk_agent.appsync.chat_repository import canonical_chat_payload
from omnidesk_agent.appsync.strict_json_store import (
    CorruptAppSyncState,
    StrictJsonAppSyncStore,
)


def test_chat_payload_fingerprint_ignores_transport_only_stream_flag() -> None:
    streaming = canonical_chat_payload(
        {"content": "hello", "stream": True}, "conv-1"
    )
    non_streaming = canonical_chat_payload(
        {"content": "hello", "stream": False}, "conv-1"
    )
    assert streaming == non_streaming == {
        "content": "hello",
        "conversation_id": "conv-1",
    }


def test_strict_json_store_quarantines_corrupt_state(tmp_path: Path) -> None:
    state = tmp_path / "appsync.json"
    state.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CorruptAppSyncState, match="quarantined"):
        StrictJsonAppSyncStore(state)

    assert not state.exists()
    quarantined = list(tmp_path.glob("appsync.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not-json"
