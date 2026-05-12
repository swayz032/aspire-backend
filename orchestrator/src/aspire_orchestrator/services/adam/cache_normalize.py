"""Query normalization for materials search — Pass C.

Design:
  - normalize_query() returns str | NormalizeRejection.
  - Rejection (not exception) on: empty, too long (>500), PII email, PII address.
  - PII detection uses regex — no external calls, no budget spent.
  - Normalised form: strip, lowercase, collapse whitespace.

Law #3: Fail closed — PII queries are rejected before any provider call.
Law #9: PII never logged or cached — rejection code only in receipts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class QueryRejectionCode(str, Enum):
    QUERY_EMPTY = "QUERY_EMPTY"
    QUERY_TOO_LONG = "QUERY_TOO_LONG"
    CONTAINS_PII_EMAIL = "CONTAINS_PII_EMAIL"
    CONTAINS_PII_ADDRESS = "CONTAINS_PII_ADDRESS"


@dataclass(frozen=True)
class NormalizeRejection:
    """Returned by normalize_query() when the query cannot be used."""

    code: QueryRejectionCode
    # Short human-readable reason safe to surface in API error; no raw PII.
    reason: str


_MAX_LEN = 500

# Email pattern — catches user@domain.tld forms
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

# Address PII pattern — catches patterns like "123 Main St" / "456 Oak Ave"
# Anchored to digit(s) + word(s) + abbreviation. Avoids false positives on
# product SKUs like "1/2 in drywall".
_ADDRESS_RE = re.compile(
    r"\b\d{2,6}\s+[A-Za-z]+(\s+[A-Za-z]+)*\s+"
    r"(Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Lane|Ln|"
    r"Drive|Dr|Court|Ct|Place|Pl|Way|Circle|Cir|Trail|Trl|"
    r"Highway|Hwy|Parkway|Pkwy|Suite|Ste|Apt)\b",
    re.IGNORECASE,
)


def normalize_query(raw: str) -> str | NormalizeRejection:
    """Normalise a search query string.

    Returns:
      str — the cleaned query ready for cache key + provider call.
      NormalizeRejection — the reason the query was rejected.

    Never raises. Callers must test with isinstance(result, NormalizeRejection).
    """
    if not raw or not raw.strip():
        return NormalizeRejection(
            code=QueryRejectionCode.QUERY_EMPTY,
            reason="Query must not be empty",
        )

    # Length check BEFORE PII check (prevents timing side-channel on PII)
    if len(raw) > _MAX_LEN:
        return NormalizeRejection(
            code=QueryRejectionCode.QUERY_TOO_LONG,
            reason=f"Query exceeds {_MAX_LEN} character limit",
        )

    if _EMAIL_RE.search(raw):
        return NormalizeRejection(
            code=QueryRejectionCode.CONTAINS_PII_EMAIL,
            reason="Query contains an email address",
        )

    if _ADDRESS_RE.search(raw):
        return NormalizeRejection(
            code=QueryRejectionCode.CONTAINS_PII_ADDRESS,
            reason="Query appears to contain a street address",
        )

    # Normalise: strip, lowercase, collapse internal whitespace
    return " ".join(raw.strip().lower().split())
