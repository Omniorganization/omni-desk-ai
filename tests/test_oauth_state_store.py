from __future__ import annotations
from omnidesk_agent.oauth.state_store import OAuthStateStore


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
