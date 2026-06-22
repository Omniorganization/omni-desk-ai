from __future__ import annotations
from pathlib import Path
from omnidesk_agent.config import GmailConfig
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager


def test_gmail_oauth_paths(tmp_path: Path):
    cfg = GmailConfig(credentials_file=tmp_path / "credentials.json", token_file=tmp_path / "token.json")
    mgr = GmailOAuthManager(cfg)
    assert cfg.allow_compose is False
    assert cfg.encrypt_token_at_rest is True
    assert "https://www.googleapis.com/auth/gmail.compose" not in mgr.scopes
    assert not mgr.credentials_available()
    mgr.save_token_json({"token": "x"})
    assert mgr.token_available()
    assert mgr.load_token_json()["token"] == "x"
