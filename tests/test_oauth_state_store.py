from __future__ import annotations
import time
from omnidesk_agent.oauth.state_store import OAuthStateStore
from omnidesk_agent.storage.sqlite import connect_sqlite


def test_oauth_state_single_use(tmp_path):
    store = OAuthStateStore(tmp_path / "states.sqlite3", ttl_seconds=60)
    state = store.create("http://localhost/callback")
    assert store.verify_and_use(state, "http://localhost/callback")
    assert not store.verify_and_use(state, "http://localhost/callback")


def test_oauth_state_binds_redirect_and_actor(tmp_path):
    store = OAuthStateStore(tmp_path / "states.sqlite3", ttl_seconds=60)
    state = store.create("http://localhost/callback", actor="alice")

    assert not store.verify_and_use(state, "http://localhost/other", actor="alice")
    assert not store.verify_and_use(state, "http://localhost/callback", actor="bob")
    assert store.verify_and_use(state, "http://localhost/callback", actor="alice")
    assert not store.verify_and_use(state, "http://localhost/callback", actor="alice")


def test_oauth_state_store_purges_expired_and_used_rows(tmp_path):
    store = OAuthStateStore(tmp_path / "states.sqlite3", ttl_seconds=60)
    expired = store.create("http://localhost/expired", actor="alice")
    used = store.create("http://localhost/used", actor="alice")
    keep = store.create("http://localhost/keep", actor="alice")
    now = time.time()

    with connect_sqlite(tmp_path / "states.sqlite3") as con:
        con.execute("UPDATE oauth_states SET created_at = ? WHERE state = ?", (now - 120.0, expired))
        con.execute("UPDATE oauth_states SET created_at = ? WHERE state = ?", (now, used))
        con.execute("UPDATE oauth_states SET created_at = ? WHERE state = ?", (now, keep))
    assert store.verify_and_use(used, "http://localhost/used", actor="alice")

    assert store.purge_expired(now=now) == 2
    assert not store.verify_and_use(expired, "http://localhost/expired", actor="alice")
    assert not store.verify_and_use(used, "http://localhost/used", actor="alice")
    assert store.verify_and_use(keep, "http://localhost/keep", actor="alice")
