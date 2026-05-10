"""Shared services for the Aspire orchestrator.

- dlp: Presidio DLP redaction
- token_service: Capability token minting and validation
- receipt_service: Receipt chain writing and verification
- policy_service: Policy engine HTTP client
"""

# Pre-import submodules used by lazy "from aspire_orchestrator.services import X"
# statements inside other modules (e.g. elevenlabs_ingestion.py enrichment block).
# Without this, those imports raise ImportError at runtime even though the files
# exist on disk -- the installed package __init__ doesn't auto-discover submodules.
from . import (  # noqa: F401
    call_logger,
    contact_writer,
    voicemail_notifier,
    voicemail_writer,
)
