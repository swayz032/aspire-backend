"""BlueprintMissingInput schema — gaps requiring contractor input."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintMissingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    project_id: UUID
    description: str | None = None
    suggested_resolution: str | None = None
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    created_by: UUID | None = None
