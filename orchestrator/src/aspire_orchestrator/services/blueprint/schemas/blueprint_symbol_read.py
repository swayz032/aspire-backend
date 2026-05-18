"""BlueprintSymbolRead — read projection for the Takeoff tab.

Lighter than BlueprintSymbol (strips suite_id, office_id, created_by — all
Internal implementation details not needed by the desktop client).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintSymbolRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    id: UUID
    sheet_id: UUID
    # DB column is `class`; Pydantic alias avoids the Python reserved word.
    class_: str | None = None
    bbox: dict[str, Any] | None = None
    confidence: float | None = None
    created_at: datetime
