"""Compliance Mapper — Map subprocessor compliance requirements.

Tracks which external providers handle what data and their compliance status.
Used by Ava Admin to show compliance posture to business owners.

Providers and their compliance:
- Stripe: PCI-DSS Level 1, SOC2 Type II
- Gusto: SOC2 Type II, HIPAA
- QuickBooks: SOC1/SOC2 Type II
- PandaDoc: SOC2 Type II
- LiveKit: SOC2 Type II
- Twilio: SOC2 Type II, HIPAA
- Moov: PCI-DSS, SOC2 Type II
- Plaid: SOC2 Type II

Aspire-owned controls:
- RBAC (via Supabase Auth + RLS)
- Log retention (receipt immutability)
- Incident handling (incident_ops service)
- Vendor management (this service)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SubprocessorEntry:
    """A single external subprocessor and its compliance posture."""

    provider: str
    data_categories: list[str]  # financial, personal, communications
    certifications: list[str]
    dpa_signed: bool
    last_audit: str  # ISO date
    risk_tier: str  # green/yellow/red


@dataclass
class ComplianceReport:
    """Generated compliance posture report (Law #2: always has a receipt)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    subprocessors: list[SubprocessorEntry] = field(default_factory=list)
    aspire_controls: list[dict[str, Any]] = field(default_factory=list)
    overall_status: str = "compliant"  # compliant, warning, non_compliant
    receipt: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Static subprocessor registry
# ---------------------------------------------------------------------------

SUBPROCESSORS: list[SubprocessorEntry] = [
    SubprocessorEntry(
        "stripe", ["financial"], ["PCI-DSS Level 1", "SOC2 Type II"],
        True, "2026-01-15", "green",
    ),
    SubprocessorEntry(
        "gusto", ["financial", "personal"], ["SOC2 Type II", "HIPAA"],
        True, "2025-12-01", "green",
    ),
    SubprocessorEntry(
        "quickbooks", ["financial"], ["SOC1 Type II", "SOC2 Type II"],
        True, "2025-11-01", "green",
    ),
    SubprocessorEntry(
        "pandadoc", ["personal", "legal"], ["SOC2 Type II"],
        True, "2025-10-15", "green",
    ),
    SubprocessorEntry(
        "livekit", ["communications"], ["SOC2 Type II"],
        True, "2025-09-01", "green",
    ),
    SubprocessorEntry(
        "twilio", ["communications", "personal"], ["SOC2 Type II", "HIPAA"],
        True, "2025-11-15", "green",
    ),
    SubprocessorEntry(
        "moov", ["financial"], ["PCI-DSS", "SOC2 Type II"],
        True, "2026-01-01", "green",
    ),
    SubprocessorEntry(
        "plaid", ["financial"], ["SOC2 Type II"],
        True, "2025-10-01", "green",
    ),
    SubprocessorEntry(
        "deepgram", ["communications"], ["SOC2 Type II"],
        True, "2025-08-01", "green",
    ),
    SubprocessorEntry(
        "elevenlabs", ["communications"], ["SOC2 Type II"],
        True, "2025-09-15", "green",
    ),
    SubprocessorEntry(
        "brave_search", ["none"], [],
        False, "", "yellow",
    ),
    SubprocessorEntry(
        "tavily", ["none"], [],
        False, "", "yellow",
    ),
]


ASPIRE_CONTROLS: list[dict[str, Any]] = [
    {"control": "RBAC", "implementation": "Supabase Auth + RLS", "status": "active"},
    {"control": "Log Retention", "implementation": "Receipt immutability (append-only)", "status": "active"},
    {"control": "Incident Handling", "implementation": "incident_ops service + runbooks", "status": "active"},
    {"control": "Vendor Management", "implementation": "compliance_mapper service", "status": "active"},
    {"control": "Data Encryption", "implementation": "TLS 1.3 in transit, AES-256 at rest (Supabase)", "status": "active"},
    {"control": "Access Audit", "implementation": "Receipt trail for all operations", "status": "active"},
    {"control": "PII Redaction", "implementation": "Presidio DLP in receipt_write", "status": "active"},
]


def generate_compliance_report(*, correlation_id: str = "") -> ComplianceReport:
    """Generate a compliance posture report.

    Always produces a receipt (Law #2).  The report is read-only (Law #7).
    """
    # Detect any non-green subprocessors → warning
    warnings = [s for s in SUBPROCESSORS if s.risk_tier != "green"]
    status = "compliant" if not warnings else "warning"

    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "action_type": "compliance.report",
        "outcome": "success",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return ComplianceReport(
        subprocessors=SUBPROCESSORS,
        aspire_controls=ASPIRE_CONTROLS,
        overall_status=status,
        receipt=receipt,
    )
