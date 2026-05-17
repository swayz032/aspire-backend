"""BlueprintStory schema — phased plain-English narrative."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintStory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    project_id: UUID
    phase: int | None = None
    markdown: str | None = None
    truth_distribution: dict[str, Any] | None = None
    supersedes_id: UUID | None = None
    created_at: datetime
    created_by: UUID | None = None
