"""BlueprintStoryRead — read projection for the Scope tab (story section).

The story is the phased plain-English narrative produced by Wave 4 REASON.
Only the active version (supersedes_id IS NULL) is returned.

Law #9: markdown may contain contractor-facing descriptions.  The router
returns the full markdown to the authenticated desktop client but must NEVER
log or embed the markdown body inside receipts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintStoryPhase(BaseModel):
    """One phase within a blueprint story."""

    model_config = ConfigDict(extra="forbid", strict=False)

    phase_number: int
    markdown: str
    truth_distribution: dict[str, Any] | None = None


class BlueprintStoryRead(BaseModel):
    """Aggregated story read model returned to the desktop client."""

    model_config = ConfigDict(extra="forbid", strict=False)

    project_id: UUID
    phases: list[BlueprintStoryPhase]
    mean_confidence: float | None = None
    model_version: str | None = None
    generated_at: datetime | None = None
