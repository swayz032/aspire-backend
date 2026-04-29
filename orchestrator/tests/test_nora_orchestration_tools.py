"""Tests for nora_orchestration_tools.py — 6 Nora orchestration tool wrappers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity


TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
MEMORY_ID = uuid.uuid4()
NOW_ISO = datetime.now(tz=timezone.utc).isoformat()


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _a2a_ok(task_id: str = "task-1") -> AsyncMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"task_id": task_id, "status": "created"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)
    return mock_client


def _a2a_timeout() -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    return mock_client


def _fake_memory_out() -> MagicMock:
    mo = MagicMock()
    mo.memory_id = MEMORY_ID
    mo.linked_receipt_ids = [uuid.uuid4()]
    return mo


# ---------------------------------------------------------------------------
# invoke_adam
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_adam_returns_dispatch_out() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraDispatchOut,
        invoke_adam,
    )

    with patch("httpx.AsyncClient", return_value=_a2a_ok("adam-task-1")):
        result = await invoke_adam(_scope(), query="research painters in Atlanta")

    assert isinstance(result, NoraDispatchOut)
    assert result.pack_id == "adam"
    assert result.task_id == "adam-task-1"
    assert result.correlation_id


@pytest.mark.asyncio
async def test_invoke_adam_timeout_raises_retryable() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraToolError,
        invoke_adam,
    )

    with patch("httpx.AsyncClient", return_value=_a2a_timeout()):
        with pytest.raises(NoraToolError) as exc_info:
            await invoke_adam(_scope(), query="research")

    assert exc_info.value.code == "PROVIDER_TIMEOUT"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_invoke_adam_invalid_scope_raises() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraToolError,
        invoke_adam,
    )

    with pytest.raises(NoraToolError) as exc_info:
        await invoke_adam(None, query="research")  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# invoke_quinn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_quinn_returns_dispatch_out() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraDispatchOut,
        invoke_quinn,
    )

    with patch("httpx.AsyncClient", return_value=_a2a_ok("quinn-task-1")):
        result = await invoke_quinn(_scope(), action="check_overdue")

    assert isinstance(result, NoraDispatchOut)
    assert result.pack_id == "quinn"


# ---------------------------------------------------------------------------
# invoke_clara
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_clara_returns_dispatch_out() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraDispatchOut,
        invoke_clara,
    )

    with patch("httpx.AsyncClient", return_value=_a2a_ok("clara-task-1")):
        result = await invoke_clara(_scope(), action="contract_review")

    assert isinstance(result, NoraDispatchOut)
    assert result.pack_id == "clara"


# ---------------------------------------------------------------------------
# invoke_tec
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_tec_returns_dispatch_out() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraDispatchOut,
        invoke_tec,
    )

    with patch("httpx.AsyncClient", return_value=_a2a_ok("tec-task-1")):
        result = await invoke_tec(_scope(), action="generate_recap_pdf")

    assert isinstance(result, NoraDispatchOut)
    assert result.pack_id == "tec"


# ---------------------------------------------------------------------------
# post_office_message — state change, uses MemoryService
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_office_message_writes_memory() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraPostMessageOut,
        post_office_message,
    )

    fake_out = _fake_memory_out()

    with patch(
        "aspire_orchestrator.services.skillpacks.nora_orchestration_tools.MemoryService.write",
        new=AsyncMock(return_value=fake_out),
    ):
        result = await post_office_message(
            _scope(),
            recipient="eli",
            body="Meeting recap ready for review.",
            subject="Recap — Monday Standup",
        )

    assert isinstance(result, NoraPostMessageOut)
    assert result.memory_id == str(MEMORY_ID)
    assert result.correlation_id


# ---------------------------------------------------------------------------
# save_office_memory — state change, uses MemoryService
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_office_memory_writes_with_office_visibility() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraSaveMemoryOut,
        save_office_memory,
    )

    fake_out = _fake_memory_out()
    captured_envelope: list[Any] = []

    async def mock_write(envelope: Any, *, scope: Any, embed: bool) -> Any:
        captured_envelope.append(envelope)
        return fake_out

    with patch(
        "aspire_orchestrator.services.skillpacks.nora_orchestration_tools.MemoryService.write",
        side_effect=mock_write,
    ):
        result = await save_office_memory(
            _scope(),
            memory_type="session_summary",
            summary="Monday standup was productive.",
            title="Monday Standup Summary",
        )

    assert isinstance(result, NoraSaveMemoryOut)
    assert result.memory_id == str(MEMORY_ID)
    # Verify visibility_scope='office' was set
    assert captured_envelope[0].visibility_scope == "office"
    assert captured_envelope[0].provenance.source_agent == "nora"


@pytest.mark.asyncio
async def test_save_office_memory_rejects_invalid_type() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NoraToolError,
        save_office_memory,
    )

    with pytest.raises(NoraToolError) as exc_info:
        await save_office_memory(
            _scope(),
            memory_type="INVALID_TYPE",
            summary="ignored",
        )

    assert exc_info.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

def test_nora_orchestration_tools_has_6_entries() -> None:
    from aspire_orchestrator.services.skillpacks.nora_orchestration_tools import (
        NORA_ORCHESTRATION_TOOLS,
    )

    assert len(NORA_ORCHESTRATION_TOOLS) == 6
    assert "nora.orchestration.invoke_adam" in NORA_ORCHESTRATION_TOOLS
    assert "nora.orchestration.save_office_memory" in NORA_ORCHESTRATION_TOOLS
