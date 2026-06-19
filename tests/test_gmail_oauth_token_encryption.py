from __future__ import annotations

from pathlib import Path

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager


def test_gmail_oauth_token_can_be_encrypted_at_rest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_GMAIL_TOKEN_ENCRYPTION_KEY", "unit-test-gmail-token-key")
    cfg = GmailConfig(
        credentials_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token.json",
        encrypt_token_at_rest=True,
    )
    mgr = GmailOAuthManager(cfg)
    mgr.save_token_json({"refresh_token": "super-secret-refresh"})

    assert mgr.load_token_json() == {"refresh_token": "super-secret-refresh"}
    raw = cfg.token_file.read_text(encoding="utf-8")
    assert raw.startswith("enc:v1:gmail-token:")
    assert "super-secret-refresh" not in raw
