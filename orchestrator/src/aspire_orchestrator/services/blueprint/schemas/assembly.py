"""BlueprintAssembly schema — derived assemblies with truth tag."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aspire_orchestrator.services.blueprint.schemas.truth import TruthClass


class BlueprintAssembly(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    project_id: UUID
    type: str | None = None
    quantity: float | None = None
    unit: str | None = None
    truth: TruthClass
    supersedes_id: UUID | None = None
    created_at: datetime
    created_by: UUID | None = None
