"""Tool execution types — shared between tool_executor and provider clients.

Extracted to break circular import: tool_executor imports provider clients,
provider clients need ToolExecutionResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from aspire_orchestrator.models import Outcome


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result of executing a tool."""

    outcome: Outcome
    tool_id: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)
    is_stub: bool = False


# Type alias for tool executor functions
ToolExecutorFn = Callable[..., Awaitable[ToolExecutionResult]]
