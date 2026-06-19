from __future__ import annotations

import os
from pathlib import Path

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.oauth.gmail_oauth import GmailOAuthManager


def test_gmail_oauth_token_saved_private(tmp_path: Path):
    cfg = GmailConfig(credentials_file=tmp_path / "credentials.json", token_file=tmp_path / "token.json")
    mgr = GmailOAuthManager(cfg)
    mgr.save_token_json({"token": "x"})

    assert mgr.load_token_json() == {"token": "x"}
    if os.name != "nt":
        assert cfg.token_file.stat().st_mode & 0o077 == 0
