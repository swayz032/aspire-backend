"""Aspire Provider Clients — External service integrations.

Each provider client inherits from BaseProviderClient and implements:
  - Authentication (API key, OAuth2, S2S HMAC)
  - Timeout enforcement (<15s default, configurable per provider)
  - Circuit breaker (fail-open disabled, fail-closed per Law #3)
  - Receipt emission for all outcomes (Law #2)
  - PII redaction before receipt persistence (Law #9)

Provider hierarchy:
  BaseProviderClient (abstract)
    +-- ApiKeyClient (Stripe, PandaDoc, Brave, Tavily, Zoom, Twilio,
    |                 Deepgram, ElevenLabs, Google Places, TomTom, HERE,
    |                 Foursquare, Mapbox, Plaid)
    +-- OAuth2Client (QuickBooks, Gusto, Google)
    +-- NoAuthClient (OSM Overpass, Puppeteer)
    +-- S2SHmacClient (Domain Rail — already implemented in domain_rail_client.py)
"""

from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderRequest,
    ProviderResponse,
    ProviderError,
    CircuitState,
)
from aspire_orchestrator.providers.oauth2_manager import (
    OAuth2Manager,
    OAuth2Token,
    OAuth2Config,
)
from aspire_orchestrator.providers.error_codes import (
    InternalErrorCode,
    ProviderErrorCategory,
)

__all__ = [
    "BaseProviderClient",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderError",
    "CircuitState",
    "OAuth2Manager",
    "OAuth2Token",
    "OAuth2Config",
    "InternalErrorCode",
    "ProviderErrorCategory",
]
