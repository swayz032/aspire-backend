"""BlueprintSymbol schema — detected symbol geometry."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    sheet_id: UUID
    class_: str | None = None
    bbox: dict[str, Any] | None = None
    confidence: float | None = None
    created_at: datetime
    created_by: UUID | None = None
