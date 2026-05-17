"""BlueprintSheetRead — response schema for GET /v1/blueprints/projects/{project_id}/sheets."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintSheetRead(BaseModel):
    """Read-only view of a blueprint sheet (one page from the uploaded PDF)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    sheet_number: str | None = None
    discipline: str | None = None
    scale: str | None = None
    revision: str | None = None
    supersedes_id: UUID | None = None
    thumbnail_url: str | None = None
    seal_detected: bool = False
    created_at: datetime
