from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.oauth.state_store import OAuthStateStore


class GmailOAuthManager:
    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
        self.state_store = OAuthStateStore(cfg.token_file.parent / "gmail_oauth_states.sqlite3", cfg.oauth_state_ttl_seconds)

    @property
    def scopes(self) -> list[str]:
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        if getattr(self.cfg, "allow_compose", True):
            scopes.append("https://www.googleapis.com/auth/gmail.compose")
        if getattr(self.cfg, "allow_send", False):
            scopes.append("https://www.googleapis.com/auth/gmail.send")
        if getattr(self.cfg, "allow_modify", False) and not getattr(self.cfg, "readonly", True):
            scopes.append("https://www.googleapis.com/auth/gmail.modify")
        # Remove duplicates while preserving order.
        return list(dict.fromkeys(scopes))

    def _check_redirect_uri(self, redirect_uri: str) -> None:
        allowlist = getattr(self.cfg, "oauth_redirect_allowlist", []) or []
        if allowlist and redirect_uri not in allowlist:
            raise PermissionError(f"redirect_uri not in gmail.oauth_redirect_allowlist: {redirect_uri}")

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
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        flow = InstalledAppFlow.from_client_secrets_file(str(self.cfg.credentials_file), self.scopes)
        creds = flow.run_local_server(port=port)
        token = json.loads(creds.to_json())
        self.save_token_json(token)
        return token

    def build_authorization_url(self, redirect_uri: str, state: str | None = None) -> dict[str, str]:
        self._check_redirect_uri(redirect_uri)
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        state_value = state or self.state_store.create(redirect_uri)
        flow = Flow.from_client_secrets_file(str(self.cfg.credentials_file), scopes=self.scopes, redirect_uri=redirect_uri)
        auth_url, returned_state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state_value,
        )
        return {"authorization_url": auth_url, "state": returned_state}

    def exchange_code(self, code: str, redirect_uri: str, state: str | None = None) -> dict[str, Any]:
        self._check_redirect_uri(redirect_uri)
        if not state or not self.state_store.verify_and_use(state, redirect_uri):
            raise PermissionError("Invalid, expired, used, or redirect_uri-mismatched OAuth state")
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        flow = Flow.from_client_secrets_file(str(self.cfg.credentials_file), scopes=self.scopes, redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
        token = json.loads(flow.credentials.to_json())
        self.save_token_json(token)
        return token

    def build_service(self):
        if not self.token_available():
            raise RuntimeError("Gmail OAuth token is missing. Run gmail-auth first.")
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError("Install google-auth and google-api-python-client") from exc

        creds = Credentials.from_authorized_user_file(str(self.cfg.token_file), self.scopes)
        return build("gmail", "v1", credentials=creds)
