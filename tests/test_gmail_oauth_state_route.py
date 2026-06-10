from omnidesk_agent.oauth.state_store import OAuthStateStore


def test_oauth_state_is_one_time(tmp_path):
    store = OAuthStateStore(tmp_path / "states.sqlite3", ttl_seconds=60)
    state = store.create("http://localhost/callback")
    assert store.verify_and_use(state, "http://localhost/callback")
    assert not store.verify_and_use(state, "http://localhost/callback")
