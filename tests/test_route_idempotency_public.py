"""The route idempotency helpers are public, and the old private names still resolve.

Other route modules (autonomy's learning-approval routes, #248) build on them;
renaming must not break those importers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from omnidesk_agent.security.idempotency import SQLiteSideEffectIdempotencyStore
from omnidesk_agent.server_routes import agent_routes
from omnidesk_agent.server_routes.idempotency import idempotent_side_effect, require_idempotency_key


def _request(headers: dict[str, str]):
    return SimpleNamespace(headers=headers)


def test_private_aliases_are_the_public_helpers():
    assert agent_routes._require_idempotency_key is require_idempotency_key
    assert agent_routes._idempotent_side_effect is idempotent_side_effect


def test_require_idempotency_key():
    assert require_idempotency_key(_request({"idempotency-key": " k1 "})) == "k1"
    assert require_idempotency_key(_request({}), "body-key") == "body-key"
    with pytest.raises(HTTPException) as missing:
        require_idempotency_key(_request({}))
    assert missing.value.status_code == 428
    with pytest.raises(HTTPException) as too_long:
        require_idempotency_key(_request({"idempotency-key": "k" * 257}))
    assert too_long.value.status_code == 422


def test_idempotent_side_effect_replays_and_refuses_a_changed_payload(tmp_path):
    rt = SimpleNamespace(side_effect_idempotency=SQLiteSideEffectIdempotencyStore(tmp_path / "idem.sqlite3"))
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        return {"ok": True, "n": calls["n"]}

    async def run(payload):
        return await idempotent_side_effect(rt, route="/x", actor="a", key="k1", payload=payload, call=call)

    first = asyncio.run(run({"v": 1}))
    replay = asyncio.run(run({"v": 1}))
    assert first == replay == {"ok": True, "n": 1} and calls["n"] == 1
    with pytest.raises(HTTPException) as changed:
        asyncio.run(run({"v": 2}))
    assert changed.value.status_code == 409
