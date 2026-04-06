"""PlaybookContext — Shared context dataclass for all playbook execute functions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlaybookContext:
    """Execution context passed to every playbook function."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None
    tenant_id: str = ""
