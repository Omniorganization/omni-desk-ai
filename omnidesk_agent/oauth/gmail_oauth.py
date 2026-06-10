from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnidesk_agent.config import GmailConfig


class GmailOAuthManager:
    """Gmail OAuth helper.

    It supports two modes:
    1. Installed-app local authorization via google-auth-oauthlib.
    2. FastAPI routes returning an authorization URL and accepting a callback code.

    Required optional dependencies:
      pip install google-auth google-auth-oauthlib google-api-python-client
    """

    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
        self.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

    def credentials_available(self) -> bool:
        return self.cfg.credentials_file.exists()

    def token_available(self) -> bool:
        return self.cfg.token_file.exists()

    def load_token_json(self) -> dict[str, Any] | None:
        if not self.cfg.token_file.exists():
            return None
        return json.loads(self.cfg.token_file.read_text(encoding="utf-8"))

    def save_token_json(self, token: dict[str, Any]) -> None:
        self.cfg.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.token_file.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_local_flow(self, port: int = 0) -> dict[str, Any]:
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        flow = InstalledAppFlow.from_client_secrets_file(str(self.cfg.credentials_file), self.scopes)
        creds = flow.run_local_server(port=port)
        token = json.loads(creds.to_json())
        self.save_token_json(token)
        return token

    def build_authorization_url(self, redirect_uri: str, state: str = "omnidesk-gmail") -> dict[str, str]:
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import Flow  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        flow = Flow.from_client_secrets_file(
            str(self.cfg.credentials_file),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        auth_url, state_value = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return {"authorization_url": auth_url, "state": state_value}

    def exchange_code(self, code: str, redirect_uri: str, state: str | None = None) -> dict[str, Any]:
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import Flow  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        flow = Flow.from_client_secrets_file(
            str(self.cfg.credentials_file),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        token = json.loads(flow.credentials.to_json())
        self.save_token_json(token)
        return token

    def build_service(self):
        if not self.token_available():
            raise RuntimeError("Gmail OAuth token is missing. Run gmail-auth first.")
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install google-auth and google-api-python-client") from exc

        creds = Credentials.from_authorized_user_file(str(self.cfg.token_file), self.scopes)
        return build("gmail", "v1", credentials=creds)
