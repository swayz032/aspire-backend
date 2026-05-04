"""Twilio Trust Hub REST API client — Wave 2-C.

Thin HTTP wrapper around the Twilio Trust Hub v1 API.  All calls use
`services/resilience.py` `resilient_call` with `TWILIO_RETRY` + `twilio_breaker()`.

API surface covered:
  - Policy SID fetchers (cached module-level)
  - Secondary Customer Profile CRUD + submission + status poll
  - EndUser creation (all types: authorized_rep, cnam_information, business_information)
  - EntityAssignment (link end-user / sub-resource to profile or trust product)
  - ChannelEndpointAssignment (attach/detach phone numbers)
  - TrustProduct CRUD + submission + status poll  (SHAKEN, CNAM, Voice Integrity, Branded)
  - IncomingPhoneNumbers helpers (caller-id lookup, friendly name update)
  - A2P 10DLC stubs (W7 — minimal; W7 author may extend)

Aspire Laws enforced:
  Law #3  — fail closed: missing credentials → TrustHubError immediately.
  Law #9  — no PII in error messages or logs (dob, ssn_last4, phone numbers redacted).
  Law #10 — circuit breaker + retry on every external HTTP call.

Base URL: https://trusthub.twilio.com/v1
Auth: HTTP Basic (account_sid:auth_token)

Idempotency: Twilio honours the `Idempotency-Key` HTTP header on POST calls.
  Callers (the state machine) own the key; this client passes it through.
  Never auto-generate inside this module.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.resilience import (
    TWILIO_RETRY,
    CircuitOpenError,
    RetryableError,
    resilient_call,
    twilio_breaker,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRUST_HUB_BASE = "https://trusthub.twilio.com/v1"
_TWILIO_BASE = "https://api.twilio.com/2010-04-01"
_TIMEOUT_SECONDS = 4.5  # <5s per Law #10 reliability standard

# Known CNAM policy SID — verified from Twilio CNAM docs (plan §II locked decisions).
_CNAM_POLICY_SID_KNOWN = "RNf3db3cd1fe25fcfd3c3ded065c8fea53"

# Module-level policy SID cache — fetched once at startup, never changes.
_POLICY_CACHE: dict[str, str] = {}

# Phone-number prefix regex for Law #9 log redaction
_PHONE_PREFIX_RE = re.compile(r"(\+?\d{1,3}\d{3})\d{4,}")


# ---------------------------------------------------------------------------
# Error class  (mirrors TwilioProvisioningError exactly — plan §W2-C)
# ---------------------------------------------------------------------------


class TrustHubError(Exception):
    """Raised on Twilio Trust Hub API failures."""

    def __init__(self, code: str, message: str, status_code: int = 0) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _twilio_auth() -> tuple[str, str]:
    """Return (account_sid, auth_token).  Fail closed per Law #3."""
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    if not sid or not token:
        raise TrustHubError(
            "MISSING_TWILIO_CREDENTIALS",
            "twilio_account_sid or twilio_auth_token not configured. Fail-closed per Law #3.",
        )
    return sid, token


def _raise_trust_hub_error(operation: str, resp: httpx.Response) -> None:
    """Parse Twilio error body and raise TrustHubError.

    Law #9: never include request PII (dob, ssn_last4, email, phone) in error.
    """
    detail = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("detail") or ""
            code = body.get("code", "")
            detail = f"{code}: {msg}".strip(": ") if code else str(msg) or detail
    except Exception:
        pass
    logger.error(
        "trust_hub op=%s status=%d detail=%s",
        operation,
        resp.status_code,
        detail,
    )
    raise TrustHubError(
        f"TRUST_HUB_{operation.upper()}_FAILED",
        f"Twilio Trust Hub {operation} failed: {detail}",
        resp.status_code,
    )


def _is_retryable_twilio_status(status_code: int) -> bool:
    """429 throttling, 5xx server errors — safe to retry.  Never 4xx."""
    return status_code == 429 or 500 <= status_code < 600


def _redact_phone(value: str) -> str:
    """Redact all but the first 7 digits of any E.164 number for logging."""
    return _PHONE_PREFIX_RE.sub(r"\1***", value)


# ---------------------------------------------------------------------------
# Low-level HTTP helpers  (inner functions passed to resilient_call)
# ---------------------------------------------------------------------------


async def _th_get(
    *,
    account_sid: str,
    auth_token: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET against the Trust Hub base URL."""
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.get(url, params=params or {})
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Trust Hub GET transient {resp.status_code}",
            )
        _raise_trust_hub_error("get", resp)
    return resp.json()


async def _th_post(
    *,
    account_sid: str,
    auth_token: str,
    url: str,
    data: dict[str, Any],
    idempotency_key: str = "",
) -> dict[str, Any]:
    """POST to Trust Hub or Twilio REST API.

    Idempotency-Key header is passed through when provided; caller owns the key.
    """
    headers: dict[str, str] = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.post(url, data=data, headers=headers)
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Trust Hub POST transient {resp.status_code}",
            )
        _raise_trust_hub_error("post", resp)
    return resp.json()


async def _th_put(
    *,
    account_sid: str,
    auth_token: str,
    url: str,
    data: dict[str, Any],
    idempotency_key: str = "",
) -> dict[str, Any]:
    """PUT to Trust Hub API."""
    headers: dict[str, str] = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.put(url, data=data, headers=headers)
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Trust Hub PUT transient {resp.status_code}",
            )
        _raise_trust_hub_error("put", resp)
    return resp.json()


async def _th_delete(
    *,
    account_sid: str,
    auth_token: str,
    url: str,
) -> int:
    """DELETE against Trust Hub API. Returns status code; 404 = already gone."""
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token),
        timeout=_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.delete(url)
    if resp.status_code == 404:
        return 404
    if resp.status_code >= 400:
        if _is_retryable_twilio_status(resp.status_code):
            raise RetryableError(
                "TWILIO_TRANSIENT",
                f"Trust Hub DELETE transient {resp.status_code}",
            )
        _raise_trust_hub_error("delete", resp)
    return resp.status_code


def _resilient_get(
    account_sid: str,
    auth_token: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Convenience wrapper: resilient GET (idempotent=True)."""
    return resilient_call(
        _th_get,
        account_sid=account_sid,
        auth_token=auth_token,
        url=url,
        params=params,
        breaker=twilio_breaker(),
        policy=TWILIO_RETRY,
        idempotent=True,
    )


def _resilient_post(
    account_sid: str,
    auth_token: str,
    url: str,
    data: dict[str, Any],
    idempotency_key: str = "",
) -> Any:
    """Convenience wrapper: resilient POST (idempotent=False — HTTP-response path no retry)."""
    return resilient_call(
        _th_post,
        account_sid=account_sid,
        auth_token=auth_token,
        url=url,
        data=data,
        idempotency_key=idempotency_key,
        breaker=twilio_breaker(),
        policy=TWILIO_RETRY,
        idempotent=False,
    )


def _resilient_put(
    account_sid: str,
    auth_token: str,
    url: str,
    data: dict[str, Any],
    idempotency_key: str = "",
) -> Any:
    """Convenience wrapper: resilient PUT (idempotent=True — PUT is naturally idempotent)."""
    return resilient_call(
        _th_put,
        account_sid=account_sid,
        auth_token=auth_token,
        url=url,
        data=data,
        idempotency_key=idempotency_key,
        breaker=twilio_breaker(),
        policy=TWILIO_RETRY,
        idempotent=True,
    )


def _resilient_delete(
    account_sid: str,
    auth_token: str,
    url: str,
) -> Any:
    """Convenience wrapper: resilient DELETE (idempotent=True — 404=already gone)."""
    return resilient_call(
        _th_delete,
        account_sid=account_sid,
        auth_token=auth_token,
        url=url,
        breaker=twilio_breaker(),
        policy=TWILIO_RETRY,
        idempotent=True,
    )


# ---------------------------------------------------------------------------
# Policy SID fetchers — cached module-level, fetched once at startup
# ---------------------------------------------------------------------------

# Cache keys
_KEY_SECONDARY_PROFILE = "secondary_customer_profile"
_KEY_SHAKEN = "shaken_stir"
_KEY_CNAM = "cnam"
_KEY_VOICE_INTEGRITY = "voice_integrity"


async def _fetch_policy_sid_from_api(friendly_name_fragment: str) -> str | None:
    """Search Twilio TrustHub/Policies for a policy matching a name fragment.

    GET /v1/TrustHub/Policies?PageSize=100
    Returns the first matching policy's SID, or None if not found.
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/Policies"
    data = await _resilient_get(
        account_sid,
        auth_token,
        url,
        params={"PageSize": 100},
    )
    policies = data.get("results") or data.get("policies") or []
    fragment_upper = friendly_name_fragment.upper()
    for policy in policies:
        name = (policy.get("friendly_name") or policy.get("name") or "").upper()
        if fragment_upper in name:
            return policy.get("sid") or policy.get("policy_sid")
    return None


async def fetch_secondary_profile_policy_sid() -> str:
    """Fetch + cache the Secondary Customer Profile policy SID.

    Check cache → settings env var → live API (last resort).
    Raises TrustHubError if not found anywhere.
    """
    if _KEY_SECONDARY_PROFILE in _POLICY_CACHE:
        return _POLICY_CACHE[_KEY_SECONDARY_PROFILE]

    # Settings env var takes precedence over live API lookup
    env_sid = (settings.twilio_secondary_profile_policy_sid or "").strip()
    if env_sid:
        _POLICY_CACHE[_KEY_SECONDARY_PROFILE] = env_sid
        return env_sid

    sid = await _fetch_policy_sid_from_api("Secondary Customer Profile")
    if not sid:
        raise TrustHubError(
            "POLICY_SID_NOT_FOUND",
            "Secondary Customer Profile policy SID not found in Twilio TrustHub/Policies",
        )
    _POLICY_CACHE[_KEY_SECONDARY_PROFILE] = sid
    return sid


async def fetch_shaken_policy_sid() -> str:
    """Fetch + cache the SHAKEN/STIR policy SID."""
    if _KEY_SHAKEN in _POLICY_CACHE:
        return _POLICY_CACHE[_KEY_SHAKEN]

    env_sid = (settings.twilio_shaken_policy_sid or "").strip()
    if env_sid:
        _POLICY_CACHE[_KEY_SHAKEN] = env_sid
        return env_sid

    # Try multiple name fragments — Twilio names differ across regions
    for fragment in ("SHAKEN", "STIR", "SHAKEN/STIR"):
        sid = await _fetch_policy_sid_from_api(fragment)
        if sid:
            _POLICY_CACHE[_KEY_SHAKEN] = sid
            return sid

    raise TrustHubError(
        "POLICY_SID_NOT_FOUND",
        "SHAKEN/STIR policy SID not found in Twilio TrustHub/Policies",
    )


async def fetch_cnam_policy_sid() -> str:
    """Fetch + cache the CNAM policy SID.

    Known value (plan §II locked): RNf3db3cd1fe25fcfd3c3ded065c8fea53.
    Settings env var or known value used without a live API call.
    """
    if _KEY_CNAM in _POLICY_CACHE:
        return _POLICY_CACHE[_KEY_CNAM]

    env_sid = (settings.twilio_cnam_policy_sid or "").strip()
    if env_sid:
        _POLICY_CACHE[_KEY_CNAM] = env_sid
        return env_sid

    # Use the architect-verified known value — no live API needed
    _POLICY_CACHE[_KEY_CNAM] = _CNAM_POLICY_SID_KNOWN
    return _CNAM_POLICY_SID_KNOWN


async def fetch_voice_integrity_policy_sid() -> str:
    """Fetch + cache the Voice Integrity policy SID."""
    if _KEY_VOICE_INTEGRITY in _POLICY_CACHE:
        return _POLICY_CACHE[_KEY_VOICE_INTEGRITY]

    env_sid = (settings.twilio_voice_integrity_policy_sid or "").strip()
    if env_sid:
        _POLICY_CACHE[_KEY_VOICE_INTEGRITY] = env_sid
        return env_sid

    for fragment in ("Voice Integrity", "VOICE_INTEGRITY", "Voice"):
        sid = await _fetch_policy_sid_from_api(fragment)
        if sid:
            _POLICY_CACHE[_KEY_VOICE_INTEGRITY] = sid
            return sid

    raise TrustHubError(
        "POLICY_SID_NOT_FOUND",
        "Voice Integrity policy SID not found in Twilio TrustHub/Policies",
    )


# ---------------------------------------------------------------------------
# Customer Profile (Secondary)
# ---------------------------------------------------------------------------


async def create_secondary_customer_profile(
    *,
    suite_id: str,
    legal_name: str,
    email: str,
    policy_sid: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a Secondary Customer Profile in Twilio Trust Hub.

    POST /v1/TrustHub/CustomerProfiles

    StatusCallback is set from settings.trust_hub_status_callback_url so Twilio
    webhooks Aspire on approval/rejection.

    Law #9: email is NOT logged; suite_id prefix only.
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles"

    payload: dict[str, Any] = {
        "FriendlyName": f"Aspire-{suite_id[:8]}-{legal_name[:40]}",
        "Email": email,
        "PolicySid": policy_sid,
        "StatusCallback": settings.trust_hub_status_callback_url or "",
        "StatusCallbackMethod": "POST",
    }

    logger.info(
        "trust_hub create_secondary_customer_profile suite=%s...",
        suite_id[:8],
    )

    try:
        result = await _resilient_post(account_sid, auth_token, url, payload, idempotency_key)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_secondary_customer_profile rejected ({ce})",
            503,
        ) from ce

    logger.info(
        "trust_hub create_secondary_customer_profile suite=%s... sid=%s status=%s",
        suite_id[:8],
        result.get("sid", ""),
        result.get("status", ""),
    )
    return result


async def submit_customer_profile(bundle_sid: str, *, idempotency_key: str) -> dict[str, Any]:
    """Submit a Secondary Customer Profile for Twilio review.

    PUT /v1/TrustHub/CustomerProfiles/{Sid} with Status=pending-review
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}"

    logger.info("trust_hub submit_customer_profile bundle_sid=%s", bundle_sid)

    try:
        result = await _resilient_put(
            account_sid,
            auth_token,
            url,
            {"Status": "pending-review"},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — submit_customer_profile rejected ({ce})",
            503,
        ) from ce

    return result


async def fetch_customer_profile_status(bundle_sid: str) -> str:
    """Poll the status of a Secondary Customer Profile.

    GET /v1/TrustHub/CustomerProfiles/{Sid}
    Returns status string: "draft"|"pending-review"|"twilio-approved"|"twilio-rejected"|"in-review"
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}"

    try:
        data = await _resilient_get(account_sid, auth_token, url)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — fetch_customer_profile_status rejected ({ce})",
            503,
        ) from ce

    return str(data.get("status", ""))


# ---------------------------------------------------------------------------
# End Users
# ---------------------------------------------------------------------------


async def create_end_user(
    *,
    profile_sid: str,
    end_user_type: str,
    attributes: dict[str, Any],
    friendly_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create an EndUser resource in Twilio Trust Hub.

    POST /v1/TrustHub/EndUsers

    end_user_type values (Twilio-defined):
      'authorized_representative_1'
      'authorized_representative_2'
      'cnam_information'         → attributes: {"cnam_display_name": "<15char>"}
      'business_information'

    For authorized_representative types, attributes includes:
      first_name, last_name, business_title, email, phone_number, dob
      (dob is decrypted at call site from vault; NEVER stored in this return value)

    Law #9: attributes are NOT logged.  Only friendly_name prefix is logged.
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/EndUsers"

    # Twilio expects Attributes as a JSON string in form-encoded body
    import json as _json

    payload: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Type": end_user_type,
        "Attributes": _json.dumps(attributes),
    }

    logger.info(
        "trust_hub create_end_user type=%s friendly_name_prefix=%s...",
        end_user_type,
        friendly_name[:12],
    )

    try:
        result = await _resilient_post(account_sid, auth_token, url, payload, idempotency_key)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_end_user rejected ({ce})",
            503,
        ) from ce

    logger.info(
        "trust_hub create_end_user sid=%s type=%s",
        result.get("sid", ""),
        end_user_type,
    )
    return result


# ---------------------------------------------------------------------------
# Entity assignments
# ---------------------------------------------------------------------------


async def assign_entity_to_profile(
    bundle_sid: str,
    entity_sid: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Link an EndUser or sub-resource to a Secondary Customer Profile.

    POST /v1/TrustHub/CustomerProfiles/{Sid}/EntityAssignments
    body: ObjectSid={entity_sid}
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}/EntityAssignments"

    logger.info(
        "trust_hub assign_entity_to_profile bundle=%s entity=%s",
        bundle_sid,
        entity_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {"ObjectSid": entity_sid},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — assign_entity_to_profile rejected ({ce})",
            503,
        ) from ce

    return result


async def assign_entity_to_trust_product(
    trust_product_sid: str,
    entity_sid: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Link an EndUser or sub-resource to a Trust Product (SHAKEN / CNAM / etc.).

    POST /v1/TrustHub/TrustProducts/{Sid}/EntityAssignments
    body: ObjectSid={entity_sid}
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{trust_product_sid}/EntityAssignments"

    logger.info(
        "trust_hub assign_entity_to_trust_product product=%s entity=%s",
        trust_product_sid,
        entity_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {"ObjectSid": entity_sid},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — assign_entity_to_trust_product rejected ({ce})",
            503,
        ) from ce

    return result


# ---------------------------------------------------------------------------
# Channel endpoint assignments (phone number attach/detach)
# ---------------------------------------------------------------------------


async def assign_number_to_profile(
    bundle_sid: str,
    number_sid: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Attach a phone number to a Secondary Customer Profile.

    POST /v1/TrustHub/CustomerProfiles/{Sid}/ChannelEndpointAssignments
    body: ChannelEndpointType=phone-number, ChannelEndpointSid={number_sid}
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}/ChannelEndpointAssignments"

    logger.info(
        "trust_hub assign_number_to_profile bundle=%s number_sid=%s",
        bundle_sid,
        number_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {
                "ChannelEndpointType": "phone-number",
                "ChannelEndpointSid": number_sid,
            },
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — assign_number_to_profile rejected ({ce})",
            503,
        ) from ce

    return result


async def add_phone_to_trust_product(
    trust_product_sid: str,
    number_sid: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Attach a phone number to a Trust Product (SHAKEN / CNAM).

    POST /v1/TrustHub/TrustProducts/{Sid}/ChannelEndpointAssignments
    body: ChannelEndpointType=phone-number, ChannelEndpointSid={number_sid}
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{trust_product_sid}/ChannelEndpointAssignments"

    logger.info(
        "trust_hub add_phone_to_trust_product product=%s number_sid=%s",
        trust_product_sid,
        number_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {
                "ChannelEndpointType": "phone-number",
                "ChannelEndpointSid": number_sid,
            },
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — add_phone_to_trust_product rejected ({ce})",
            503,
        ) from ce

    return result


async def list_channel_endpoint_assignments(
    bundle_sid: str,
    *,
    kind: Literal["customer_profile", "trust_product"],
) -> list[dict[str, Any]]:
    """List all ChannelEndpointAssignments for a bundle.

    kind='customer_profile' → GET /v1/TrustHub/CustomerProfiles/{Sid}/ChannelEndpointAssignments
    kind='trust_product'    → GET /v1/TrustHub/TrustProducts/{Sid}/ChannelEndpointAssignments
    """
    account_sid, auth_token = _twilio_auth()

    if kind == "customer_profile":
        url = f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}/ChannelEndpointAssignments"
    else:
        url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{bundle_sid}/ChannelEndpointAssignments"

    try:
        data = await _resilient_get(account_sid, auth_token, url)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — list_channel_endpoint_assignments rejected ({ce})",
            503,
        ) from ce

    return data.get("results") or data.get("channel_endpoint_assignments") or []


async def delete_channel_endpoint_assignment(
    bundle_sid: str,
    ra_sid: str,
    *,
    kind: Literal["customer_profile", "trust_product"],
) -> None:
    """Remove a ChannelEndpointAssignment (detach a phone number from a bundle).

    W11 (number swap) uses this to detach before re-attaching to the new number.
    404 = already detached — treated as success (idempotent).

    kind='customer_profile' → DELETE /v1/TrustHub/CustomerProfiles/{Sid}/ChannelEndpointAssignments/{RaSid}
    kind='trust_product'    → DELETE /v1/TrustHub/TrustProducts/{Sid}/ChannelEndpointAssignments/{RaSid}
    """
    account_sid, auth_token = _twilio_auth()

    if kind == "customer_profile":
        url = (
            f"{_TRUST_HUB_BASE}/TrustHub/CustomerProfiles/{bundle_sid}"
            f"/ChannelEndpointAssignments/{ra_sid}"
        )
    else:
        url = (
            f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{bundle_sid}"
            f"/ChannelEndpointAssignments/{ra_sid}"
        )

    logger.info(
        "trust_hub delete_channel_endpoint_assignment bundle=%s ra=%s kind=%s",
        bundle_sid,
        ra_sid,
        kind,
    )

    try:
        status = await _resilient_delete(account_sid, auth_token, url)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — delete_channel_endpoint_assignment rejected ({ce})",
            503,
        ) from ce

    if status not in (200, 204, 404):
        raise TrustHubError(
            "TRUST_HUB_DELETE_FAILED",
            f"Trust Hub DELETE returned unexpected status {status}",
            status,
        )


# ---------------------------------------------------------------------------
# Trust Products (SHAKEN, CNAM, Voice Integrity, Branded Calling)
# ---------------------------------------------------------------------------


async def create_trust_product(
    *,
    friendly_name: str,
    email: str,
    policy_sid: str,
    status_callback: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a Trust Product (SHAKEN / CNAM / Voice Integrity / Branded Calling).

    POST /v1/TrustHub/TrustProducts

    StatusCallback is set from settings.trust_hub_status_callback_url if not
    explicitly provided; Twilio webhooks Aspire on approval/rejection.

    Law #9: email is NOT logged.
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts"

    callback_url = status_callback or settings.trust_hub_status_callback_url or ""

    payload: dict[str, Any] = {
        "FriendlyName": friendly_name,
        "Email": email,
        "PolicySid": policy_sid,
        "StatusCallback": callback_url,
        "StatusCallbackMethod": "POST",
    }

    logger.info(
        "trust_hub create_trust_product friendly_name=%s policy=%s",
        friendly_name[:40],
        policy_sid,
    )

    try:
        result = await _resilient_post(account_sid, auth_token, url, payload, idempotency_key)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_trust_product rejected ({ce})",
            503,
        ) from ce

    logger.info(
        "trust_hub create_trust_product sid=%s status=%s",
        result.get("sid", ""),
        result.get("status", ""),
    )
    return result


async def submit_trust_product(bundle_sid: str, *, idempotency_key: str) -> dict[str, Any]:
    """Submit a Trust Product for Twilio review.

    PUT /v1/TrustHub/TrustProducts/{Sid} with Status=pending-review
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{bundle_sid}"

    logger.info("trust_hub submit_trust_product bundle_sid=%s", bundle_sid)

    try:
        result = await _resilient_put(
            account_sid,
            auth_token,
            url,
            {"Status": "pending-review"},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — submit_trust_product rejected ({ce})",
            503,
        ) from ce

    return result


async def fetch_trust_product_status(bundle_sid: str) -> str:
    """Poll the status of a Trust Product.

    GET /v1/TrustHub/TrustProducts/{Sid}
    Returns status string: "draft"|"pending-review"|"twilio-approved"|"twilio-rejected"|"in-review"
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TRUST_HUB_BASE}/TrustHub/TrustProducts/{bundle_sid}"

    try:
        data = await _resilient_get(account_sid, auth_token, url)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — fetch_trust_product_status rejected ({ce})",
            503,
        ) from ce

    return str(data.get("status", ""))


# ---------------------------------------------------------------------------
# IncomingPhoneNumbers helpers  (W5 Step 9 + W11 swap)
# ---------------------------------------------------------------------------


async def enable_caller_id_lookup(number_sid: str, *, idempotency_key: str) -> dict[str, Any]:
    """Enable VoiceCallerIdLookup on an IncomingPhoneNumber.

    POST /2010-04-01/Accounts/{AccountSid}/IncomingPhoneNumbers/{Sid}.json
    body: VoiceCallerIdLookup=true

    Law #9: number_sid is non-PII (it's a Twilio SID, not the E.164 number).
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"

    logger.info("trust_hub enable_caller_id_lookup number_sid=%s", number_sid)

    try:
        result = await _resilient_put(
            account_sid,
            auth_token,
            url,
            {"VoiceCallerIdLookup": "true"},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — enable_caller_id_lookup rejected ({ce})",
            503,
        ) from ce

    return result


async def disable_caller_id_lookup(number_sid: str, *, idempotency_key: str) -> None:
    """Disable VoiceCallerIdLookup on an IncomingPhoneNumber.

    POST /2010-04-01/Accounts/{AccountSid}/IncomingPhoneNumbers/{Sid}.json
    body: VoiceCallerIdLookup=false
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"

    logger.info("trust_hub disable_caller_id_lookup number_sid=%s", number_sid)

    try:
        await _resilient_put(
            account_sid,
            auth_token,
            url,
            {"VoiceCallerIdLookup": "false"},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — disable_caller_id_lookup rejected ({ce})",
            503,
        ) from ce


async def update_phone_number_friendly_name(
    number_sid: str,
    friendly_name: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Update the FriendlyName of an IncomingPhoneNumber.

    POST /2010-04-01/Accounts/{AccountSid}/IncomingPhoneNumbers/{Sid}.json
    body: FriendlyName=...

    Used by W11 (number swap) to re-label after swap.
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"

    logger.info(
        "trust_hub update_phone_number_friendly_name number_sid=%s name=%s",
        number_sid,
        friendly_name[:40],
    )

    try:
        result = await _resilient_put(
            account_sid,
            auth_token,
            url,
            {"FriendlyName": friendly_name},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — update_phone_number_friendly_name rejected ({ce})",
            503,
        ) from ce

    return result


async def release_phone_number(number_sid: str) -> None:
    """Release a phone number back to Twilio (W11 number swap).

    DELETE /2010-04-01/Accounts/{AccountSid}/IncomingPhoneNumbers/{Sid}.json

    After a swap completes, the old number is no longer attached to any
    Trust Hub bundle and `VoiceCallerIdLookup` has been disabled. This
    final DELETE removes it from the Twilio account so the tenant stops
    paying for it. 404 is treated as success (idempotent — already
    released by an earlier retry).

    Law #9: number_sid is non-PII (Twilio SID, not E.164).
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"

    logger.info("trust_hub release_phone_number number_sid=%s", number_sid)

    try:
        status = await _resilient_delete(account_sid, auth_token, url)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — release_phone_number rejected ({ce})",
            503,
        ) from ce

    if status not in (200, 204, 404):
        raise TrustHubError(
            "TRUST_HUB_DELETE_FAILED",
            f"IncomingPhoneNumber DELETE returned unexpected status {status}",
            status,
        )


# ---------------------------------------------------------------------------
# A2P 10DLC  (W7 minimal stubs — W7 author extends)
# ---------------------------------------------------------------------------

_TWILIO_MESSAGING_BASE = "https://messaging.twilio.com/v1"


async def create_a2p_brand_registration(
    *,
    customer_profile_sid: str,
    a2p_profile_sid: str,
    sole_prop: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    """Register an A2P Brand with Twilio.

    POST /v1/a2p/BrandRegistrations
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_MESSAGING_BASE}/a2p/BrandRegistrations"

    payload: dict[str, Any] = {
        "CustomerProfileBundleSid": customer_profile_sid,
        "A2PProfileBundleSid": a2p_profile_sid,
        "IsMain": "true",
        "SkipAutomaticSecProgReg": "false",
    }
    if sole_prop:
        payload["BrandType"] = "SOLE_PROPRIETOR"

    logger.info(
        "trust_hub create_a2p_brand_registration profile=%s sole_prop=%s",
        customer_profile_sid,
        sole_prop,
    )

    try:
        result = await _resilient_post(account_sid, auth_token, url, payload, idempotency_key)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_a2p_brand_registration rejected ({ce})",
            503,
        ) from ce

    return result


async def create_messaging_service(
    *,
    friendly_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a Twilio Messaging Service.

    POST /v1/Services
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_MESSAGING_BASE}/Services"

    logger.info("trust_hub create_messaging_service name=%s", friendly_name[:40])

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {"FriendlyName": friendly_name},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_messaging_service rejected ({ce})",
            503,
        ) from ce

    return result


async def add_phone_to_messaging_service(
    messaging_service_sid: str,
    number_sid: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Add an IncomingPhoneNumber to a Messaging Service.

    POST /v1/Services/{Sid}/PhoneNumbers
    """
    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_MESSAGING_BASE}/Services/{messaging_service_sid}/PhoneNumbers"

    logger.info(
        "trust_hub add_phone_to_messaging_service service=%s number_sid=%s",
        messaging_service_sid,
        number_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {"PhoneNumberSid": number_sid},
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — add_phone_to_messaging_service rejected ({ce})",
            503,
        ) from ce

    return result


async def create_sole_proprietor_vetting(
    *,
    brand_registration_sid: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Submit a Sole Proprietor Vetting request for an A2P brand.

    POST /v1/a2p/BrandRegistrations/{Sid}/SoleProprietorVettings

    This endpoint is called after the OTP is confirmed by the tenant (W8).
    It formally triggers Twilio's free Sole Prop vetting flow.

    Law #9: no PII in parameters — brand_registration_sid is a Twilio SID.
    """
    account_sid, auth_token = _twilio_auth()
    url = (
        f"{_TWILIO_MESSAGING_BASE}/a2p/BrandRegistrations"
        f"/{brand_registration_sid}/SoleProprietorVettings"
    )

    logger.info(
        "trust_hub create_sole_proprietor_vetting brand_reg_sid=%s",
        brand_registration_sid,
    )

    try:
        result = await _resilient_post(
            account_sid, auth_token, url, {}, idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_sole_proprietor_vetting rejected ({ce})",
            503,
        ) from ce

    logger.info(
        "trust_hub create_sole_proprietor_vetting sid=%s status=%s",
        result.get("sid", ""),
        result.get("status", ""),
    )
    return result


async def submit_a2p_otp(
    *,
    brand_registration_sid: str,
    otp_code: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Submit an OTP code to verify a Sole Proprietor A2P brand registration.

    POST /v1/a2p/BrandRegistrations/{Sid}/OtpVerifications

    Twilio sends an OTP SMS to the authorized rep's phone after BrandRegistrations
    POST. The tenant enters the code in the W8 UI; this endpoint submits it.

    Law #9: otp_code is NOT logged (it's a 6-digit ephemeral credential).
    """
    account_sid, auth_token = _twilio_auth()
    url = (
        f"{_TWILIO_MESSAGING_BASE}/a2p/BrandRegistrations"
        f"/{brand_registration_sid}/OtpVerifications"
    )

    # otp_code is the only PII-adjacent field — log only the SID, never the code.
    logger.info(
        "trust_hub submit_a2p_otp brand_reg_sid=%s",
        brand_registration_sid,
    )

    try:
        result = await _resilient_post(
            account_sid,
            auth_token,
            url,
            {"Otp": otp_code},  # Law #9: otp_code used here and discarded; never logged
            idempotency_key,
        )
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — submit_a2p_otp rejected ({ce})",
            503,
        ) from ce

    return result


async def create_a2p_campaign(
    *,
    messaging_service_sid: str,
    description: str,
    message_samples: list[str],
    use_case: str,
    has_embedded_links: bool,
    has_embedded_phone: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    """Register an A2P 10DLC campaign.

    POST /v1/a2p/UsAppToPerson
    """
    import json as _json

    account_sid, auth_token = _twilio_auth()
    url = f"{_TWILIO_MESSAGING_BASE}/a2p/UsAppToPerson"

    payload: dict[str, Any] = {
        "MessagingServiceSid": messaging_service_sid,
        "Description": description,
        "MessageSamples": _json.dumps(message_samples),
        "UseCase": use_case,
        "HasEmbeddedLinks": str(has_embedded_links).lower(),
        "HasEmbeddedPhone": str(has_embedded_phone).lower(),
    }

    logger.info(
        "trust_hub create_a2p_campaign service=%s use_case=%s",
        messaging_service_sid,
        use_case,
    )

    try:
        result = await _resilient_post(account_sid, auth_token, url, payload, idempotency_key)
    except CircuitOpenError as ce:
        raise TrustHubError(
            "TWILIO_CIRCUIT_OPEN",
            f"Twilio is degraded — create_a2p_campaign rejected ({ce})",
            503,
        ) from ce

    return result


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    # Error
    "TrustHubError",
    # Policy fetchers
    "fetch_secondary_profile_policy_sid",
    "fetch_shaken_policy_sid",
    "fetch_cnam_policy_sid",
    "fetch_voice_integrity_policy_sid",
    # Customer Profile
    "create_secondary_customer_profile",
    "submit_customer_profile",
    "fetch_customer_profile_status",
    # End Users
    "create_end_user",
    # Entity assignments
    "assign_entity_to_profile",
    "assign_entity_to_trust_product",
    # Channel endpoint assignments
    "assign_number_to_profile",
    "add_phone_to_trust_product",
    "list_channel_endpoint_assignments",
    "delete_channel_endpoint_assignment",
    # Trust Products
    "create_trust_product",
    "submit_trust_product",
    "fetch_trust_product_status",
    # IncomingPhoneNumbers helpers
    "enable_caller_id_lookup",
    "disable_caller_id_lookup",
    "update_phone_number_friendly_name",
    "release_phone_number",
    # A2P 10DLC
    "create_a2p_brand_registration",
    "create_sole_proprietor_vetting",
    "submit_a2p_otp",
    "create_messaging_service",
    "add_phone_to_messaging_service",
    "create_a2p_campaign",
    # Policy cache (exposed for test injection)
    "_POLICY_CACHE",
    "_CNAM_POLICY_SID_KNOWN",
]
