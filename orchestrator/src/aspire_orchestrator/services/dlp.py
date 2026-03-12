"""DLP Service — Presidio PII Redaction (Law #9, Gate 5).

Per CLAUDE.md Law #9 (Security & Privacy Baselines):
  - Never log secrets. Never store provider keys in repo.
  - Redact PII in logs/receipts when possible (use Presidio DLP).

PII Redaction Rules:
  - Social Security Numbers → <SSN_REDACTED>
  - Credit card numbers → <CC_REDACTED>
  - Email addresses → <EMAIL_REDACTED>
  - Phone numbers → <PHONE_REDACTED>
  - Physical addresses → <ADDRESS_REDACTED>
  - Person names → <PERSON_REDACTED>

Integration points:
  - receipt_write_node: redact PII from receipt payloads before chain hashing
  - respond_node: redact PII from AvaResult plan/execution_result before egress

Per policy_matrix.yaml, each action specifies `redact_fields` — the DLP service
processes those fields plus always scans for PII in free-text fields.

Fail-closed: If Presidio fails to initialize, redaction is skipped with a
logged warning. Per policy_engine defaults, fail_closed_on_dlp_error=True
means execution is denied if DLP fails on YELLOW/RED tier operations.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Entities we scan for (maps to Presidio recognizer types)
_SCAN_ENTITIES = [
    "US_SSN",
    "CREDIT_CARD",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "PERSON",
    "LOCATION",
    "US_BANK_NUMBER",
    "IBAN_CODE",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
]

# Redaction labels per CLAUDE.md Law #9
_REDACTION_MAP = {
    "US_SSN": "<SSN_REDACTED>",
    "CREDIT_CARD": "<CC_REDACTED>",
    "EMAIL_ADDRESS": "<EMAIL_REDACTED>",
    "PHONE_NUMBER": "<PHONE_REDACTED>",
    "PERSON": "<PERSON_REDACTED>",
    "LOCATION": "<ADDRESS_REDACTED>",
    "US_BANK_NUMBER": "<BANK_ACCT_REDACTED>",
    "IBAN_CODE": "<IBAN_REDACTED>",
    "US_PASSPORT": "<PASSPORT_REDACTED>",
    "US_DRIVER_LICENSE": "<DL_REDACTED>",
}

# Minimum confidence score to redact (0.0-1.0)
_MIN_SCORE = 0.4
_REGEX_PATTERNS = (
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL_ADDRESS", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
    ("PHONE_NUMBER", re.compile(r"\+?\d[\d\s\-().]{7,}\d")),
)

# Receipt fields that should be scanned for PII
_RECEIPT_TEXT_FIELDS = frozenset({
    "error_message",
    "reason_code",
    "redacted_inputs",
    "redacted_outputs",
})

# Fields that should NEVER be redacted (structural/governance fields)
_RECEIPT_PROTECTED_FIELDS = frozenset({
    "id",
    "correlation_id",
    "suite_id",
    "office_id",
    "chain_id",
    "sequence",
    "receipt_hash",
    "previous_receipt_hash",
    "actor_type",
    "actor_id",
    "action_type",
    "risk_tier",
    "tool_used",
    "capability_token_id",
    "capability_token_hash",
    "receipt_type",
    "outcome",
    "created_at",
    "approved_at",
    "executed_at",
})


class DLPInitializationError(Exception):
    """Raised when DLP is required but not available (Law #3: fail-closed)."""


class DLPService:
    """Presidio-based DLP service for PII redaction.

    Lazy-initialized to avoid startup cost when DLP is not needed.
    Thread-safe after initialization.
    """

    def __init__(self) -> None:
        self._analyzer = None
        self._anonymizer = None
        self._initialized = False
        self._init_error: str | None = None
        self._regex_fallback = False

    def _ensure_initialized(self) -> bool:
        """Lazy-init Presidio engines. Returns True if ready."""
        if self._initialized:
            return self._init_error is None or self._regex_fallback

        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine

            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
            self._initialized = True
            self._regex_fallback = False
            logger.info("DLP service initialized (Presidio analyzer + anonymizer)")
            return True

        except Exception as e:
            self._initialized = True
            self._init_error = str(e)
            self._regex_fallback = True
            logger.warning(
                "DLP service initialization failed, falling back to regex redaction: %s",
                e,
            )
            return True

    def _regex_redact_text(self, text: str) -> str:
        redacted = text
        for entity_type, pattern in _REGEX_PATTERNS:
            label = _REDACTION_MAP[entity_type]
            redacted = pattern.sub(label, redacted)
        return redacted

    def redact_text(self, text: str) -> str:
        """Redact PII from a text string using Presidio.

        Returns the redacted text with PII replaced by type-specific placeholders.
        If Presidio is unavailable, returns the original text with a warning.
        """
        if not text or not isinstance(text, str):
            return text

        if not self._ensure_initialized():
            logger.warning("DLP unavailable, returning unredacted text")
            return text

        if self._regex_fallback:
            return self._regex_redact_text(text)

        try:
            from presidio_anonymizer.entities import OperatorConfig

            results = self._analyzer.analyze(
                text=text,
                entities=_SCAN_ENTITIES,
                language="en",
                score_threshold=_MIN_SCORE,
            )

            if not results:
                return text

            operators = {
                entity_type: OperatorConfig("replace", {"new_value": label})
                for entity_type, label in _REDACTION_MAP.items()
            }

            anonymized = self._anonymizer.anonymize(
                text=text,
                analyzer_results=results,
                operators=operators,
            )

            if anonymized.text != text:
                entity_types = {r.entity_type for r in results}
                logger.info(
                    "DLP redacted %d entities: %s",
                    len(results), sorted(entity_types),
                )

            return anonymized.text

        except Exception as e:
            logger.error("DLP redaction failed: %s", e)
            return text

    def redact_dict(
        self,
        data: dict[str, Any],
        *,
        fields: list[str] | None = None,
        protected_fields: frozenset[str] = _RECEIPT_PROTECTED_FIELDS,
    ) -> dict[str, Any]:
        """Redact PII from string values in a dict.

        Args:
            data: The dict to scan
            fields: Specific fields to scan (None = scan all string fields)
            protected_fields: Fields to never modify
        """
        if not data:
            return data

        result = dict(data)

        for key, value in result.items():
            if key in protected_fields:
                continue

            if fields and key not in fields:
                continue

            if isinstance(value, str) and len(value) > 3:
                result[key] = self.redact_text(value)
            elif isinstance(value, dict):
                result[key] = self.redact_dict(
                    value, fields=fields, protected_fields=protected_fields,
                )

        return result

    def redact_receipt(
        self,
        receipt: dict[str, Any],
        *,
        redact_fields: list[str] | None = None,
        fail_closed: bool = False,
    ) -> dict[str, Any]:
        """Redact PII from a receipt dict before chain hashing.

        Applies two passes:
        1. Scan policy-specified redact_fields (from policy_matrix.yaml)
        2. Scan standard receipt text fields for any remaining PII

        Protected fields (IDs, hashes, timestamps) are never modified.

        Args:
            fail_closed: If True (YELLOW/RED tier), raises DLPInitializationError
                if DLP is unavailable. If False (GREEN tier), returns unredacted.
        """
        if fail_closed:
            self.require_available()
        if not receipt:
            return receipt

        result = dict(receipt)

        # Pass 1: Policy-specified fields
        if redact_fields:
            for field in redact_fields:
                if field in result and isinstance(result[field], str):
                    result[field] = self.redact_text(result[field])

        # Pass 2: Standard text fields
        for field in _RECEIPT_TEXT_FIELDS:
            if field in result and isinstance(result[field], str):
                result[field] = self.redact_text(result[field])

        return result

    def redact_receipts(
        self,
        receipts: list[dict[str, Any]],
        *,
        redact_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Redact PII from a list of receipts."""
        return [
            self.redact_receipt(r, redact_fields=redact_fields)
            for r in receipts
        ]

    @property
    def available(self) -> bool:
        """Check if DLP service is available."""
        return self._ensure_initialized()

    def require_available(self) -> None:
        """Assert DLP is available — raises DLPInitializationError if not.

        Use this for YELLOW/RED tier operations where PII redaction is mandatory.
        Law #3: fail-closed — missing DLP = deny execution.
        Law #2: emit denial receipt before raising (MEDIUM-01 fix).
        """
        available = self._ensure_initialized()
        if available or self._regex_fallback:
            return
        if not available:
            # Emit denial receipt before raising (Law #2)
            try:
                import uuid as _uuid
                from datetime import datetime as _dt, timezone as _tz
                from aspire_orchestrator.services.receipt_store import store_receipts
                denial_receipt = {
                    "id": str(_uuid.uuid4()),
                    "correlation_id": "dlp_init_failure",
                    "suite_id": "system",
                    "office_id": "system",
                    "actor_type": "system",
                    "actor_id": "dlp_service",
                    "action_type": "dlp.require_available",
                    "risk_tier": "green",
                    "tool_used": "dlp.presidio",
                    "created_at": _dt.now(_tz.utc).isoformat(),
                    "outcome": "DENIED",
                    "reason_code": "DLP_UNAVAILABLE",
                    "error_message": f"DLP not available: {self._init_error}",
                    "receipt_type": "policy_decision",
                    "receipt_hash": "",
                }
                store_receipts([denial_receipt])
            except Exception:
                logger.warning("Failed to store DLP denial receipt (best-effort)")
            raise DLPInitializationError(
                f"DLP service not available (Law #3 fail-closed): {self._init_error}"
            )


# Module-level singleton (lazy init)
_dlp_service: DLPService | None = None


def get_dlp_service() -> DLPService:
    """Get the DLP service singleton."""
    global _dlp_service
    if _dlp_service is None:
        _dlp_service = DLPService()
    return _dlp_service


def redact_text(text: str) -> str:
    """Convenience: redact PII from a text string."""
    return get_dlp_service().redact_text(text)


def redact_receipt(
    receipt: dict[str, Any],
    *,
    redact_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Convenience: redact PII from a receipt dict."""
    return get_dlp_service().redact_receipt(receipt, redact_fields=redact_fields)


def redact_receipts(
    receipts: list[dict[str, Any]],
    *,
    redact_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Convenience: redact PII from a list of receipts."""
    return get_dlp_service().redact_receipts(receipts, redact_fields=redact_fields)
