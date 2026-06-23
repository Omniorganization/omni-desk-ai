from __future__ import annotations

import json
import os
from typing import Any, Optional

from omnidesk_agent.config import GmailConfig
from omnidesk_agent.oauth.state_store import OAuthStateStore
from omnidesk_agent.privacy.encryption import EncryptionProvider


class GmailOAuthManager:
    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
        self.state_store = OAuthStateStore(cfg.token_file.parent / "gmail_oauth_states.sqlite3", cfg.oauth_state_ttl_seconds)
        self.encryption = (
            EncryptionProvider.from_env(cfg.token_encryption_key_env, required=bool(getattr(cfg, "enabled", False)), key_id="gmail-token")
            if getattr(cfg, "encrypt_token_at_rest", False)
            else EncryptionProvider.disabled()
        )

    @property
    def scopes(self) -> list[str]:
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        if getattr(self.cfg, "allow_compose", False):
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

    def load_token_json(self) -> Optional[dict[str, Any]]:
        if not self.cfg.token_file.exists():
            return None
        self._ensure_private_token_permissions()
        raw = self.cfg.token_file.read_text(encoding="utf-8")
        if raw.startswith(EncryptionProvider.PREFIX):
            raw = self.encryption.decrypt_text(raw) or "{}"
        return json.loads(raw)

    def save_token_json(self, token: dict[str, Any]) -> None:
        self.cfg.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.token_file.with_suffix(self.cfg.token_file.suffix + ".tmp")
        serialized = json.dumps(token, ensure_ascii=False, indent=2)
        if self.encryption.enabled:
            serialized = self.encryption.encrypt_text(serialized) or ""
        tmp.write_text(serialized, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.cfg.token_file)
        self._ensure_private_token_permissions()

    def _ensure_private_token_permissions(self) -> None:
        if not self.cfg.token_file.exists():
            return
        try:
            mode = self.cfg.token_file.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(self.cfg.token_file, 0o600)
        except OSError:
            # Windows and some mounted filesystems may reject chmod. Loading still
            # works, but production validation should flag host-level controls.
            return

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

    def build_authorization_url(self, redirect_uri: str, state: Optional[str] = None, *, actor: str | None = None) -> dict[str, str]:
        # Do not trust caller-provided state. Always create a server-side one-time state.
        self._check_redirect_uri(redirect_uri)
        if not self.credentials_available():
            raise RuntimeError(f"Gmail credentials file missing: {self.cfg.credentials_file}")
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:
            raise RuntimeError("Install google-auth-oauthlib to use Gmail OAuth flow") from exc

        state_value = self.state_store.create(redirect_uri, actor=actor)
        flow = Flow.from_client_secrets_file(str(self.cfg.credentials_file), scopes=self.scopes, redirect_uri=redirect_uri)
        auth_url, returned_state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state_value,
        )
        return {"authorization_url": auth_url, "state": returned_state}

    def exchange_code(self, code: str, redirect_uri: str, state: Optional[str] = None, *, actor: str | None = None) -> dict[str, Any]:
        self._check_redirect_uri(redirect_uri)
        if not state or not self.state_store.verify_and_use(state, redirect_uri, actor=actor):
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

        token = self.load_token_json()
        if token is None:
            raise RuntimeError("Gmail OAuth token is missing. Run gmail-auth first.")
        creds = Credentials.from_authorized_user_info(token, self.scopes)
        return build("gmail", "v1", credentials=creds)
