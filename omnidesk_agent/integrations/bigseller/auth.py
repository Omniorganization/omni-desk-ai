from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerAuthenticationError
from omnidesk_agent.integrations.bigseller.schemas import BigSellerTokenState


class BigSellerTokenManager:
    def __init__(self, token_state: BigSellerTokenState):
        self.token_state = token_state

    @classmethod
    def from_config(cls, config: BigSellerConfig) -> "BigSellerTokenManager":
        return cls(
            BigSellerTokenState(
                access_token=config.access_token,
                refresh_token=config.refresh_token,
                expires_at=config.token_expires_at,
            )
        )

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        expires_at = self.token_state.expires_at
        if expires_at is None:
            return not bool(self.token_state.access_token)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc) + timedelta(
            seconds=skew_seconds
        )

    def ensure_access_token(self, refresh: Callable[[], BigSellerTokenState]) -> str:
        if self.is_expired():
            self.replace(refresh())
        token = self.token_state.access_token
        if not token:
            raise BigSellerAuthenticationError(
                "BigSeller access token is not configured"
            )
        return token

    def force_refresh(self, refresh: Callable[[], BigSellerTokenState]) -> str:
        self.replace(refresh())
        token = self.token_state.access_token
        if not token:
            raise BigSellerAuthenticationError(
                "BigSeller refresh did not return an access token"
            )
        return token

    def replace(self, token_state: BigSellerTokenState) -> None:
        self.token_state = token_state

    def redacted(self) -> dict[str, object]:
        return {
            "access_token_configured": bool(self.token_state.access_token),
            "refresh_token_configured": bool(self.token_state.refresh_token),
            "expires_at": self.token_state.expires_at.isoformat()
            if self.token_state.expires_at
            else None,
            "expired": self.is_expired(),
        }
