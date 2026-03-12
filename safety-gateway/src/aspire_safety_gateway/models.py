from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SafetyCheckRequest(BaseModel):
    task_type: str
    suite_id: str
    office_id: str
    payload: Any = None


class SafetyCheckResponse(BaseModel):
    allowed: bool
    reason: str | None = None
    source: str = "local"
    matched_rule: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
