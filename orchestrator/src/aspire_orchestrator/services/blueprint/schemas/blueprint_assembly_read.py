"""BlueprintAssemblyRead — read projection for the Scope tab."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aspire_orchestrator.services.blueprint.schemas.truth import TruthClass


class BlueprintAssemblyRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    id: UUID
    type: str | None = None
    quantity: float | None = None
    unit: str | None = None
    truth: TruthClass
    supersedes_id: UUID | None = None
    created_at: datetime
