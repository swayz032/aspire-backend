"""BlueprintProject schema — top-level project row."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintProject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    address: str | None = None
    created_at: datetime
    created_by: UUID | None = None
