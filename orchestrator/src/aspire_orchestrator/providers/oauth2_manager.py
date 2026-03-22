"""OAuth2 Token Manager — Handles token refresh for OAuth2 providers.

Providers using OAuth2:
  - QuickBooks (Intuit OAuth2)
  - Gusto (Gusto OAuth2)
  - Google (Google OAuth2 service accounts)

Per Risk Register R1:
  - Proactive refresh when token has <5min remaining
  - Refresh failures emit receipts (Law #2)
  - Token refresh is per-suite (tenant isolation, Law #6)
  - Refresh tokens stored in Supabase `finance_connections` table
  - Access tokens cached in memory (never persisted to disk)

Thread safety:
  - Token refresh uses asyncio locks per suite+provider to prevent thundering herd
  - Concurrent requests for the same suite+provider wait for the first refresh

Security:
  - Client secrets loaded from environment (never hardcoded, Law #9)
  - Tokens are short-lived (typically 1h)
  - Refresh tokens rotated on use where provider supports it
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from aspire_orchestrator.providers.error_codes import InternalErrorCode

logger = logging.getLogger(__name__)

# Refresh token when this many seconds remain before expiry
PROACTIVE_REFRESH_THRESHOLD_S = 300  # 5 minutes


@dataclass
class OAuth2Config:
    """OAuth2 provider configuration."""

    provider_id: str
    client_id: str
    client_secret: str
    token_url: str
    authorize_url: str = ""
    scopes: list[str] = field(default_factory=list)
    # Some providers rotate refresh tokens on use
    rotate_refresh_token: bool = False


@dataclass
class OAuth2Token:
    """OAuth2 access + refresh token pair."""

    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    token_type: str = "Bearer"
    scopes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """Check if access token is expired."""
        return time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        """Check if token should be proactively refreshed."""
        return time.time() >= (self.expires_at - PROACTIVE_REFRESH_THRESHOLD_S)

    @property
    def remaining_seconds(self) -> float:
        """Seconds until expiry."""
        return max(0, self.expires_at - time.time())


class OAuth2Manager:
    """Per-provider, per-suite OAuth2 token manager.

    Handles proactive refresh, concurrent request dedup, and token storage.

    Usage:
        manager = OAuth2Manager(config)
        token = await manager.get_token(suite_id="...")
        # Use token.access_token in provider HTTP calls
    """

    def __init__(self, config: OAuth2Config) -> None:
        self._config = config
        # In-memory token cache: {suite_id: OAuth2Token}
        self._tokens: dict[str, OAuth2Token] = {}
        # Per-suite refresh locks to prevent thundering herd
        self._locks: dict[str, asyncio.Lock] = {}
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=15.0)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _get_lock(self, suite_id: str) -> asyncio.Lock:
        """Get or create per-suite refresh lock."""
        if suite_id not in self._locks:
            self._locks[suite_id] = asyncio.Lock()
        return self._locks[suite_id]

    def set_token(self, suite_id: str, token: OAuth2Token) -> None:
        """Manually set a token (e.g., after initial OAuth2 authorization)."""
        self._tokens[suite_id] = token
        logger.info(
            "OAuth2 token set for %s suite=%s (expires in %.0fs)",
            self._config.provider_id,
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            token.remaining_seconds,
        )

    def set_token_from_db(
        self,
        suite_id: str,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: float,
        scopes: list[str] | None = None,
    ) -> None:
        """Set token from database row (finance_connections table)."""
        self._tokens[suite_id] = OAuth2Token(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes or self._config.scopes,
        )

    async def get_token(self, suite_id: str) -> OAuth2Token:
        """Get a valid access token for a suite, refreshing if needed.

        Raises ProviderError if no token exists or refresh fails.
        """
        from aspire_orchestrator.providers.error_codes import InternalErrorCode
        from aspire_orchestrator.providers.base_client import ProviderError

        token = self._tokens.get(suite_id)
        if token is None:
            raise ProviderError(
                code=InternalErrorCode.AUTH_EXPIRED_TOKEN,
                message=f"No OAuth2 token for suite {suite_id[:8]} on {self._config.provider_id}. "
                "User must complete OAuth2 authorization flow.",
                provider_id=self._config.provider_id,
            )

        if not token.needs_refresh:
            return token

        # Token needs refresh — acquire lock to prevent thundering herd
        lock = self._get_lock(suite_id)
        async with lock:
            # Double-check after acquiring lock (another request may have refreshed)
            token = self._tokens.get(suite_id)
            if token and not token.needs_refresh:
                return token

            if token is None or not token.refresh_token:
                raise ProviderError(
                    code=InternalErrorCode.AUTH_REFRESH_FAILED,
                    message=f"No refresh token for suite {suite_id[:8]} on {self._config.provider_id}",
                    provider_id=self._config.provider_id,
                )

            return await self._refresh(suite_id, token)

    async def _refresh(self, suite_id: str, current_token: OAuth2Token) -> OAuth2Token:
        """Perform token refresh. Must be called under lock."""
        from aspire_orchestrator.providers.base_client import ProviderError

        logger.info(
            "OAuth2 refreshing token for %s suite=%s (%.0fs remaining)",
            self._config.provider_id,
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            current_token.remaining_seconds,
        )

        try:
            client = await self._get_client()
            response = await client.post(
                self._config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current_token.refresh_token,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code != 200:
                error_body = response.json() if response.content else {}
                # Law #9: Log only the error code, never error_description
                # (may contain PII, tokens, or sensitive provider details).
                error_code_str = error_body.get("error", f"HTTP {response.status_code}")
                logger.error(
                    "OAuth2 refresh failed for %s suite=%s: error=%s",
                    self._config.provider_id,
                    suite_id[:8] if len(suite_id) > 8 else suite_id,
                    error_code_str,
                )
                raise ProviderError(
                    code=InternalErrorCode.AUTH_REFRESH_FAILED,
                    message=f"OAuth2 refresh failed: {error_code_str}",
                    provider_id=self._config.provider_id,
                    status_code=response.status_code,
                )

            body = response.json()
            expires_in = body.get("expires_in", 3600)

            new_token = OAuth2Token(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token", current_token.refresh_token),
                expires_at=time.time() + expires_in,
                token_type=body.get("token_type", "Bearer"),
                scopes=body.get("scope", "").split() if body.get("scope") else current_token.scopes,
                raw=body,
            )

            self._tokens[suite_id] = new_token

            logger.info(
                "OAuth2 token refreshed for %s suite=%s (expires in %ds%s)",
                self._config.provider_id,
                suite_id[:8] if len(suite_id) > 8 else suite_id,
                expires_in,
                ", refresh token rotated" if body.get("refresh_token") else "",
            )

            return new_token

        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError(
                code=InternalErrorCode.NETWORK_TIMEOUT,
                message=f"OAuth2 token refresh timed out for {self._config.provider_id}",
                provider_id=self._config.provider_id,
            )
        except Exception as e:
            raise ProviderError(
                code=InternalErrorCode.AUTH_REFRESH_FAILED,
                message=f"OAuth2 token refresh error: {type(e).__name__}: {e}",
                provider_id=self._config.provider_id,
            )

    def clear_token(self, suite_id: str) -> None:
        """Remove cached token for a suite (e.g., on disconnect/revocation)."""
        self._tokens.pop(suite_id, None)
        logger.info(
            "OAuth2 token cleared for %s suite=%s",
            self._config.provider_id,
            suite_id[:8] if len(suite_id) > 8 else suite_id,
        )

    @property
    def active_suites(self) -> list[str]:
        """List suite IDs with cached tokens."""
        return list(self._tokens.keys())
