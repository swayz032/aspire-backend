"""BlueprintProjectRead — response schema for GET /v1/blueprints/projects/{project_id}."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintProjectRead(BaseModel):
    """Read-only view of a blueprint project for the frontend polling loop."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    address: str | None = None
    created_at: datetime
    created_by: UUID | None = None
    stage_progress: dict[str, str]
    sheet_count: int
