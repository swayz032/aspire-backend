"""BlueprintProjectStatus — response schema for GET /v1/blueprints/projects/{project_id}/status.

Polled by the desktop frontend every 2s while any stage is `in_progress`.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BlueprintProjectStatus(BaseModel):
    """Lightweight status snapshot for frontend polling."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    stage_progress: dict[str, str]
    updated_at: datetime
    sheet_count: int
    symbol_count: int
    missing_input_count: int
