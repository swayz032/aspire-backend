"""BlueprintMissingInputRead — read projection for the Scope tab (gaps section)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintMissingInputRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    id: UUID
    description: str | None = None
    suggested_resolution: str | None = None
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None
    created_at: datetime
