"""AWS Secrets Manager bootstrap for Aspire Orchestrator.

Architecture:
  - On startup: fetch all secrets from SM, inject into os.environ
  - 5-minute cache TTL
  - On auth failure: invalidate cache -> re-fetch -> retry once
  - Local dev without AWS creds: fall through to .env file
  - Fail-closed: SM fetch failure in production = RuntimeError (Law #3)

Services only need 3 env vars:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
Everything else comes from SM.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# SM key name -> os.environ variable name
KEY_MAP: dict[str, str] = {
    # Stripe
    "restricted_key": "ASPIRE_STRIPE_API_KEY",
    "secret_key": "STRIPE_SECRET_KEY",
    "publishable_key": "STRIPE_PUBLISHABLE_KEY",
    "webhook_secret": "STRIPE_WEBHOOK_SECRET",
    # Supabase
    "service_role_key": "ASPIRE_SUPABASE_SERVICE_ROLE_KEY",
    "jwt_secret": "SUPABASE_JWT_SECRET",
    # Internal
    "token_signing_secret": "TOKEN_SIGNING_SECRET",
    "token_encryption_key": "TOKEN_ENCRYPTION_KEY",
    "n8n_hmac_secret": "N8N_WEBHOOK_SECRET",
    "n8n_eli_webhook_secret": "N8N_ELI_WEBHOOK_SECRET",
    "n8n_sarah_webhook_secret": "N8N_SARAH_WEBHOOK_SECRET",
    "n8n_nora_webhook_secret": "N8N_NORA_WEBHOOK_SECRET",
    "domain_rail_hmac_secret": "DOMAIN_RAIL_HMAC_SECRET",
    "gateway_internal_key": "GATEWAY_INTERNAL_KEY",
    # Providers
    "elevenlabs_key": "ELEVENLABS_API_KEY",
    "deepgram_key": "DEEPGRAM_API_KEY",
    "livekit_key": "LIVEKIT_API_KEY",
    "livekit_secret": "LIVEKIT_SECRET",
    "anam_key": "ANAM_API_KEY",
}

# Per-group overrides for colliding key names (e.g., "api_key" in both openai and twilio)
GROUP_KEY_MAP: dict[str, dict[str, str]] = {
    "openai": {"api_key": "OPENAI_API_KEY"},
    "twilio": {
        "account_sid": "TWILIO_ACCOUNT_SID",
        "api_key": "TWILIO_API_KEY",
        "api_secret": "TWILIO_API_SECRET",
        "auth_token": "TWILIO_AUTH_TOKEN",
    },
}

# Bridge: ASPIRE_-prefixed env var -> raw env var name
# Settings uses env_prefix="ASPIRE_" (Pydantic), but SM injects raw names.
# _align_settings_prefix() copies raw -> ASPIRE_ so both code paths work.
_SETTINGS_PREFIX_MAP: dict[str, str] = {
    # OpenAI
    "ASPIRE_OPENAI_API_KEY": "OPENAI_API_KEY",
    # Twilio
    "ASPIRE_TWILIO_ACCOUNT_SID": "TWILIO_ACCOUNT_SID",
    "ASPIRE_TWILIO_AUTH_TOKEN": "TWILIO_AUTH_TOKEN",
    # ElevenLabs
    "ASPIRE_ELEVENLABS_API_KEY": "ELEVENLABS_API_KEY",
    # Deepgram
    "ASPIRE_DEEPGRAM_API_KEY": "DEEPGRAM_API_KEY",
    # LiveKit
    "ASPIRE_LIVEKIT_API_KEY": "LIVEKIT_API_KEY",
    "ASPIRE_LIVEKIT_API_SECRET": "LIVEKIT_SECRET",
    # PandaDoc
    "ASPIRE_PANDADOC_API_KEY": "PANDADOC_API_KEY",
    # Anam
    "ASPIRE_ANAM_API_KEY": "ANAM_API_KEY",
}


def _align_settings_prefix() -> None:
    """Copy raw env vars to ASPIRE_-prefixed names for Pydantic Settings.

    SM injects keys like OPENAI_API_KEY, but Settings reads ASPIRE_OPENAI_API_KEY.
    This bridges the gap without breaking existing os.environ.get() callers.
    Only copies if the ASPIRE_ key is not already set (no overwrite).
    """
    for aspire_key, raw_key in _SETTINGS_PREFIX_MAP.items():
        raw_val = os.environ.get(raw_key)
        if raw_val and not os.environ.get(aspire_key):
            os.environ[aspire_key] = raw_val
            logger.debug("Bridged %s -> %s", raw_key, aspire_key)


def verify_settings_coverage() -> list[str]:
    """Check which Settings fields are still empty after secrets load.

    Returns list of empty field names. Logs warnings for each.
    """
    missing: list[str] = []
    critical_fields = [
        ("openai_api_key", "ASPIRE_OPENAI_API_KEY"),
        ("elevenlabs_api_key", "ASPIRE_ELEVENLABS_API_KEY"),
        ("deepgram_api_key", "ASPIRE_DEEPGRAM_API_KEY"),
        ("livekit_api_key", "ASPIRE_LIVEKIT_API_KEY"),
        ("twilio_account_sid", "ASPIRE_TWILIO_ACCOUNT_SID"),
        ("pandadoc_api_key", "ASPIRE_PANDADOC_API_KEY"),
    ]
    for field_name, env_var in critical_fields:
        if not os.environ.get(env_var):
            missing.append(field_name)
            logger.warning("Settings field '%s' (%s) is empty after secrets load", field_name, env_var)
    return missing


_last_fetch: float = 0
_CACHE_TTL = 300  # 5 minutes
_boto_client: Any = None


def _get_client() -> Any:
    """Lazy-create boto3 SM client (avoids import at module load)."""
    global _boto_client
    if _boto_client is None:
        import boto3
        _boto_client = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
    return _boto_client


def load_secrets() -> None:
    """Fetch all secrets from AWS SM and inject into os.environ.

    Behavior:
      - production + no AWS creds -> FATAL (fail-closed, Law #3)
      - local dev + no AWS creds -> skip, use .env file
      - cache fresh -> skip (5-min TTL)
      - SM fetch failure in production -> RuntimeError (server won't start)
    """
    global _last_fetch

    aspire_env = os.getenv("ASPIRE_ENV", "local")
    is_production = aspire_env == "production"
    has_aws_creds = bool(os.getenv("AWS_SECRET_ACCESS_KEY"))

    # Local dev without AWS creds — use .env file
    if not is_production and not has_aws_creds:
        logger.info("Local dev mode — using .env file (no AWS creds)")
        # Still bridge raw env vars → ASPIRE_-prefixed for Pydantic Settings
        _align_settings_prefix()
        return

    # Production without AWS creds — fail closed (Law #3)
    if is_production and not has_aws_creds:
        raise RuntimeError(
            "FATAL: Production mode requires AWS credentials. "
            "Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY."
        )

    # Cache still fresh — skip
    if time.time() - _last_fetch < _CACHE_TTL:
        return

    client = _get_client()
    env = "prod" if is_production else "dev"

    groups = [
        ("stripe", f"aspire/{env}/stripe"),
        ("supabase", f"aspire/{env}/supabase"),
        ("openai", f"aspire/{env}/openai"),
        ("twilio", f"aspire/{env}/twilio"),
        ("internal", f"aspire/{env}/internal"),
        ("providers", f"aspire/{env}/providers"),
    ]

    loaded_count = 0

    for group_name, path in groups:
        try:
            resp = client.get_secret_value(SecretId=path)
            secrets = json.loads(resp["SecretString"])

            for k, v in secrets.items():
                # Skip internal rotation metadata
                if k.startswith("_"):
                    continue

                # Use group-specific mapping if available
                group_map = GROUP_KEY_MAP.get(group_name, {})
                env_var = group_map.get(k) or KEY_MAP.get(k) or k.upper()

                os.environ[env_var] = str(v)
                loaded_count += 1

        except Exception as e:
            msg = f"Failed to fetch {path} from SM"
            critical_groups = {"stripe", "supabase", "internal"}
            is_critical = group_name in critical_groups

            if is_production or is_critical:
                # Fail closed — critical groups required even in dev (Law #3)
                logger.error("%s: %s", msg, type(e).__name__)
                raise RuntimeError(f"Secrets Manager fetch failed for {path}") from e
            else:
                # Dev mode non-critical group — warn and continue
                logger.warning("%s: %s", msg, type(e).__name__)

    _last_fetch = time.time()
    logger.info("Loaded %d secrets from %d SM groups (%s)", loaded_count, len(groups), env)

    # Bridge raw env vars to ASPIRE_-prefixed for Pydantic Settings
    _align_settings_prefix()


def invalidate_cache() -> None:
    """Invalidate the secrets cache — forces next load_secrets() call to re-fetch.

    Call when detecting provider auth failures (key may have been rotated).
    """
    global _last_fetch, _boto_client
    _last_fetch = 0
    _boto_client = None  # Force new client
    logger.info("Secrets cache invalidated — next load_secrets() will re-fetch from SM")


def is_secrets_manager_active() -> bool:
    """Check if secrets were loaded from SM (vs .env fallback)."""
    return _last_fetch > 0


def handle_provider_auth_error(provider: str, error: Exception) -> bool:
    """Handle provider authentication errors by invalidating cache and reloading.

    Call from any code that catches a provider auth error (e.g., Stripe
    AuthenticationError, OpenAI 401, Twilio 401). If SM is active, this
    invalidates the cache and reloads secrets.

    Returns True if secrets were reloaded (caller should retry once).
    Returns False if SM is inactive or reload failed.
    """
    # Check if this looks like an auth error
    err_str = str(error).lower()
    is_auth = any(keyword in err_str for keyword in [
        "authentication", "unauthorized", "invalid key",
        "invalid api key", "401",
    ]) or getattr(error, "http_status", None) == 401

    if not is_auth:
        return False

    if not is_secrets_manager_active():
        return False

    logger.warning(
        "Provider auth error from %s — invalidating cache and reloading from SM",
        provider,
    )
    invalidate_cache()

    try:
        load_secrets()
        logger.info("Secrets reloaded after %s auth error — caller should retry once", provider)
        return True
    except Exception as reload_err:
        logger.error(
            "Failed to reload secrets after %s auth error: %s",
            provider,
            type(reload_err).__name__,
        )
        return False
