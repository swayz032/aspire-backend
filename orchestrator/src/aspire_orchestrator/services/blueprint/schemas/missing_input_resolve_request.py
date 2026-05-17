"""MissingInputResolveRequest — body schema for POST .../resolve.

This is a YELLOW-tier state-change action (Law #4).  The capability_token
field is validated server-side by the route handler before any DB write.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class MissingInputResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    resolution_value: str
    resolved_by: UUID
    # Capability token is passed as a raw string (JWT or signed blob);
    # server-side verification happens in the route handler.
    capability_token: str

    @field_validator("resolution_value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("resolution_value must not be blank")
        return v
