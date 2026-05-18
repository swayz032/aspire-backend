"""BlueprintMaterial schema — line items with truth + tariff tag."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag, TruthClass


class BlueprintMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    suite_id: UUID
    office_id: UUID | None = None
    project_id: UUID
    line_item: str | None = None
    quantity: float | None = None
    unit: str | None = None
    truth: TruthClass
    tariff_flag: TariffFlag = TariffFlag.NONE
    supplier_id: str | None = None
    supersedes_id: UUID | None = None
    created_at: datetime
    created_by: UUID | None = None
