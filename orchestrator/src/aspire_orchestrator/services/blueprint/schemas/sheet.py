"""BlueprintSheet schema — per-sheet metadata."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aspire_orchestrator.services.blueprint.schemas.truth import Discipline


class BlueprintSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    project_id: UUID
    sheet_number: str | None = None
    discipline: Discipline | None = None
    scale: str | None = None
    revision: str | None = None
    supersedes_id: UUID | None = None
    ocr_text: str | None = None
    hash: str | None = None
    created_at: datetime
    created_by: UUID | None = None
