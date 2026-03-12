"""Safety Gateway service.

Provides a stable integration boundary for the orchestrator safety gate.
Current modes:
  - local: deterministic in-process pattern matching
  - remote: call an external safety service (for example a NeMo Guardrails sidecar)
  - off: bypass safety checks (dev-only)

The orchestrator should use this service instead of importing a specific
guardrails framework directly. That keeps the backend runtime decoupled from
heavy safety-provider dependency constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0
_JAILBREAK_PATTERNS = (
    "ignore previous instructions",
    "ignore all instructions",
    "you are now",
    "pretend you are",
    "act as if",
    "disregard your rules",
    "bypass safety",
    "ignore your guidelines",
    "forget your instructions",
    "override your programming",
    "new system prompt",
    "system: you are",
    "ignore safety",
    "jailbreak",
    "dan mode",
    "developer mode",
    "do anything now",
    "sudo mode",
    "ignore all previous",
    "disregard all previous",
    "forget all previous",
    "you must obey",
    "roleplay as",
    "simulate being",
)

_TOPIC_DENY_PATTERNS = (
    "build malware",
    "steal credentials",
    "phishing campaign",
    "exfiltrate data",
    "ransomware",
)

_client_lock = threading.Lock()
_client: httpx.Client | None = None


@dataclass(slots=True)
class SafetyDecision:
    allowed: bool
    reason: str | None = None
    source: str = "local"
    matched_rule: str | None = None
    metadata: dict[str, Any] | None = None


class SafetyGatewayError(Exception):
    """Raised when the remote safety service cannot be reached or returns invalid data."""


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(timeout=max(float(settings.safety_gateway_timeout_seconds), 0.1))
        return _client


def close_safety_gateway_client() -> None:
    global _client
    with _client_lock:
        if _client is not None and not _client.is_closed:
            _client.close()
        _client = None


def _normalize_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.lower()
    try:
        return json.dumps(payload, sort_keys=True, default=str).lower()
    except Exception:
        return str(payload).lower()


def _local_decision(payload: Any) -> SafetyDecision:
    haystack = _normalize_text(payload)

    for pattern in _JAILBREAK_PATTERNS:
        if pattern in haystack:
            return SafetyDecision(
                allowed=False,
                reason=f"Safety gateway blocked jailbreak pattern ({pattern})",
                source="local",
                matched_rule=pattern,
                metadata={"category": "jailbreak"},
            )

    for pattern in _TOPIC_DENY_PATTERNS:
        if pattern in haystack:
            return SafetyDecision(
                allowed=False,
                reason=f"Safety gateway blocked unsafe topic ({pattern})",
                source="local",
                matched_rule=pattern,
                metadata={"category": "topic"},
            )

    return SafetyDecision(allowed=True, source="local", metadata={"category": "pass"})


def _remote_decision(payload: Any, *, task_type: str, suite_id: str, office_id: str) -> SafetyDecision:
    url = (settings.safety_gateway_url or "").strip()
    if not url:
        raise SafetyGatewayError("ASPIRE_SAFETY_GATEWAY_URL is required when safety gateway mode is remote")

    body = {
        "task_type": task_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "payload": payload,
    }
    headers: dict[str, str] = {}
    shared_secret = (settings.safety_gateway_shared_secret or "").strip()
    if shared_secret:
        headers["x-safety-gateway-key"] = shared_secret

    response = _get_client().post(url, json=body, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "allowed" not in data:
        raise SafetyGatewayError("Safety gateway returned invalid response shape")

    return SafetyDecision(
        allowed=bool(data.get("allowed")),
        reason=data.get("reason"),
        source=str(data.get("source") or "remote"),
        matched_rule=data.get("matched_rule"),
        metadata=data if isinstance(data, dict) else None,
    )


def evaluate_safety(payload: Any, *, task_type: str, suite_id: str, office_id: str) -> SafetyDecision:
    mode = (settings.safety_gateway_mode or "local").strip().lower()
    fail_closed = settings.safety_gateway_fail_closed

    if mode == "off":
        return SafetyDecision(allowed=True, source="off", metadata={"category": "disabled"})

    if mode == "remote":
        try:
            return _remote_decision(payload, task_type=task_type, suite_id=suite_id, office_id=office_id)
        except Exception as exc:
            logger.error("Remote safety gateway failed: %s", exc)
            if fail_closed:
                raise SafetyGatewayError(str(exc)) from exc
            return _local_decision(payload)

    return _local_decision(payload)
