"""BlueprintMaterialRead — read projection for the Scope tab (materials section).

Law #9: supplier_id is an opaque UUID — not PII, safe to return.  The supplier
address / business name must NOT appear here (they live in the supplier table).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag, TruthClass


class BlueprintMaterialRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    id: UUID
    line_item: str | None = None
    quantity: float | None = None
    unit: str | None = None
    truth: TruthClass
    tariff_flag: TariffFlag = TariffFlag.NONE
    supplier_id: str | None = None
    supersedes_id: UUID | None = None
    created_at: datetime
