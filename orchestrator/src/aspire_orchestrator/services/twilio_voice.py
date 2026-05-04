"""Twilio Voice JWT minting + caller-id resolution.

Used by the Call Room to:
  1. Issue a short-lived Voice Access Token to the browser SDK so the
     Device can register and place an outbound call.
  2. Look up which Aspire purchased number to use as the caller_id when
     the SDK's Device hits our TwiML webhook.

Splits cleanly from `services/twilio_provisioning.py`:
  - twilio_provisioning.py owns search/purchase/release and uses the
    rotation-managed (api_key, api_secret) or (account_sid, auth_token).
  - twilio_voice.py owns the Voice SDK pair
    (voice_api_key_sid, voice_api_key_secret, twiml_app_sid). These were
    minted once when the API Key + TwiML App were provisioned and live in
    AWS SM under aspire/prod/twilio.{voice_api_key_*,twiml_app_sid}.

Law compliance:
  Law #3 — fail-closed when any of (account_sid, voice_api_key_sid,
            voice_api_key_secret, twiml_app_sid) is missing.
  Law #5 — token TTL is short (3600s) and identity is per-(suite, user)
            so multiple operators don't collide.
  Law #9 — secrets never logged. Identity is logged but doesn't include
            phone numbers or PII.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)


_DEFAULT_VOICE_TOKEN_TTL_SECONDS = 3600
_IDENTITY_PREFIX = "aspire"
# UUID + alphanumeric only — no slashes / spaces / unicode in Twilio identity.
_IDENTITY_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
_COMPACT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}[0-9a-fA-F]{4}[0-9a-fA-F]{4}[0-9a-fA-F]{4}[0-9a-fA-F]{12}$"
)


class TwilioVoiceConfigError(RuntimeError):
    """Raised when Voice SDK env vars are missing — handler fails-closed."""


def _validate_config() -> tuple[str, str, str, str]:
    """Resolve + validate the 4 secrets needed to mint a Voice JWT.

    Returns (account_sid, api_key_sid, api_key_secret, twiml_app_sid).
    Raises TwilioVoiceConfigError when any are missing — caller catches
    and returns 503 to the FE so the Return Call page can fall through
    to the receipt-only path.
    """
    sid = settings.twilio_account_sid
    api_key_sid = settings.twilio_voice_api_key_sid
    api_key_secret = settings.twilio_voice_api_key_secret
    twiml_app_sid = settings.twilio_twiml_app_sid

    missing = [
        name
        for name, val in (
            ("twilio_account_sid", sid),
            ("twilio_voice_api_key_sid", api_key_sid),
            ("twilio_voice_api_key_secret", api_key_secret),
            ("twilio_twiml_app_sid", twiml_app_sid),
        )
        if not val
    ]
    if missing:
        raise TwilioVoiceConfigError(
            "Twilio Voice SDK not fully configured — missing: " + ", ".join(missing)
        )
    return sid, api_key_sid, api_key_secret, twiml_app_sid


def build_identity(*, suite_id: str, user_id: str) -> str:
    """Stable per-(suite, user) identity used for the Voice JWT.

    Format: `aspire-{suite_id}-{user_id}` with all non-safe chars stripped
    so Twilio accepts it. Stable identity means a re-mint for the same
    user on the same browser is a no-op (Twilio won't kick out an active
    Device just because the same identity registered again with a fresh
    token).
    """
    safe_suite = re.sub(r"[^a-zA-Z0-9]", "", suite_id or "unknown")
    safe_user = re.sub(r"[^a-zA-Z0-9]", "", user_id or "anon")
    identity = f"{_IDENTITY_PREFIX}-{safe_suite}-{safe_user}"
    if not _IDENTITY_RE.match(identity):
        # Should never happen given the regex strip above but defensive.
        identity = f"{_IDENTITY_PREFIX}-fallback-{safe_user[:8]}"
    return identity


def parse_identity(identity: str) -> dict[str, str | None]:
    """Reverse `build_identity` so the TwiML webhook can resolve scope.

    Returns {'suite_id': ..., 'user_id': ...} when the identity matches
    our format, or both keys None for foreign identities (we fail-closed
    in the webhook on those).

    Twilio's voice webhook sends the SDK identity in the `From` field
    with a `client:` prefix (e.g., `client:aspire-<suite>-<user>`).
    We strip it before parsing so both the bare identity (used internally
    when minting the token) and the wrapped form work.
    """
    if not isinstance(identity, str):
        return {"suite_id": None, "user_id": None}
    # Twilio prefixes Voice SDK identities with `client:` in webhook params.
    if identity.startswith("client:"):
        identity = identity[len("client:"):]
    if not identity.startswith(_IDENTITY_PREFIX + "-"):
        return {"suite_id": None, "user_id": None}
    parts = identity.split("-", 2)
    if len(parts) != 3:
        return {"suite_id": None, "user_id": None}
    return {
        "suite_id": _restore_compact_uuid(parts[1]),
        "user_id": _restore_compact_uuid(parts[2]),
    }


def _restore_compact_uuid(value: str) -> str:
    """Restore UUID dashes stripped by build_identity before DB lookup."""
    if not isinstance(value, str) or not _COMPACT_UUID_RE.match(value):
        return value
    return (
        f"{value[0:8]}-{value[8:12]}-{value[12:16]}-"
        f"{value[16:20]}-{value[20:32]}"
    ).lower()


def mint_voice_token(
    *,
    suite_id: str,
    user_id: str,
    ttl_seconds: int = _DEFAULT_VOICE_TOKEN_TTL_SECONDS,
) -> dict[str, Any]:
    """Mint a Twilio Voice Access Token for the browser SDK Device.

    Returns: {'token': jwt_string, 'identity': 'aspire-...', 'expires_at': iso8601}.
    Raises TwilioVoiceConfigError when secrets are missing.
    """
    # Lazy import — twilio is a heavyweight dep we only load when this code
    # runs, keeping cold-start fast for non-call-related routes.
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant

    account_sid, api_key_sid, api_key_secret, twiml_app_sid = _validate_config()
    identity = build_identity(suite_id=suite_id, user_id=user_id)

    token = AccessToken(
        account_sid,
        api_key_sid,
        api_key_secret,
        identity=identity,
        ttl=ttl_seconds,
    )
    grant = VoiceGrant(
        outgoing_application_sid=twiml_app_sid,
        # Disable inbound to the SDK — Sarah handles incoming calls via her
        # own ElevenLabs leg. The Call Room is outbound-only for v1.
        incoming_allow=False,
    )
    token.add_grant(grant)

    jwt_str = token.to_jwt()
    if isinstance(jwt_str, bytes):  # twilio-python returns bytes on some versions
        jwt_str = jwt_str.decode("utf-8")

    # Compute expires_at without serializing the JWT body (avoid PyJWT dep here).
    from datetime import datetime, timezone, timedelta

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

    return {
        "token": jwt_str,
        "identity": identity,
        "expires_at": expires_at,
    }


def verify_twilio_signature(
    *,
    request_url: str,
    form_params: dict[str, str],
    signature_header: str,
) -> bool:
    """Validate a Twilio webhook signature against the master Auth Token.

    Twilio signs `request_url + sorted(param key/value pairs joined)` with
    HMAC-SHA1 using the account's primary Auth Token (not API Key secret).
    See https://www.twilio.com/docs/usage/webhooks/webhooks-security.

    Returns False on any error so the caller fail-closes with 401.
    """
    if not signature_header:
        return False
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        logger.error("twilio-python not installed; rejecting webhook")
        return False
    auth_token = settings.twilio_auth_token
    if not auth_token:
        logger.error("twilio_auth_token not configured; rejecting webhook")
        return False
    validator = RequestValidator(auth_token)
    try:
        return bool(validator.validate(request_url, form_params, signature_header))
    except Exception as e:  # noqa: BLE001
        logger.warning("verify_twilio_signature failed: %s", type(e).__name__)
        return False


def twilio_signature_url_candidates(
    *,
    received_url: str,
    forwarded_proto: str | None = None,
    forwarded_host: str | None = None,
    host: str | None = None,
) -> list[str]:
    """Return URL variants Twilio may have signed before our proxy hop."""
    candidates: list[str] = []

    def add(url: str) -> None:
        if url and url not in candidates:
            candidates.append(url)

    add(received_url)

    proto = (forwarded_proto or "").split(",")[0].strip()
    public_host = (forwarded_host or host or "").split(",")[0].strip()
    if proto and public_host:
        parsed = urlsplit(received_url)
        add(urlunsplit((proto, public_host, parsed.path, parsed.query, "")))

    return candidates
