"""Tests for WorkingMemory — within-session conversation buffer.

Covers: add_turn, get_recent_turns, get_all_turns, clear_session,
        TTL eviction, max turn cap, suite isolation (Law #6).
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.working_memory import (
    ConversationTurn,
    WorkingMemory,
    get_working_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wm():
    """Fresh WorkingMemory instance (in-memory mode, no Redis)."""
    mem = WorkingMemory()
    mem._redis_checked = True  # Skip Redis check
    mem._redis_client = None
    return mem


SUITE_A = "suite-aaa-111"
SUITE_B = "suite-bbb-222"
SESSION_1 = "sess-001"
SESSION_2 = "sess-002"


# ---------------------------------------------------------------------------
# ConversationTurn
# ---------------------------------------------------------------------------

class TestConversationTurn:
    def test_defaults(self):
        turn = ConversationTurn(role="user", content="hello")
        assert turn.role == "user"
        assert turn.content == "hello"
        assert turn.agent_id == ""
        assert turn.timestamp  # auto-generated
        assert turn.metadata == {}

    def test_explicit_fields(self):
        turn = ConversationTurn(
            role="agent", content="hi", agent_id="finn",
            timestamp="2026-01-01T00:00:00Z", metadata={"key": "val"},
        )
        assert turn.agent_id == "finn"
        assert turn.timestamp == "2026-01-01T00:00:00Z"
        assert turn.metadata["key"] == "val"


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------

class TestWorkingMemoryCRUD:
    @pytest.mark.asyncio
    async def test_add_and_get_recent(self, wm):
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="hello"))
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="agent", content="hi there"))

        turns = await wm.get_recent_turns(SESSION_1, SUITE_A, max_turns=10)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].content == "hello"
        assert turns[1].role == "agent"
        assert turns[1].content == "hi there"

    @pytest.mark.asyncio
    async def test_get_all_turns(self, wm):
        for i in range(5):
            await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content=f"msg-{i}"))

        all_turns = await wm.get_all_turns(SESSION_1, SUITE_A)
        assert len(all_turns) == 5

    @pytest.mark.asyncio
    async def test_get_recent_limits_count(self, wm):
        for i in range(10):
            await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content=f"msg-{i}"))

        recent = await wm.get_recent_turns(SESSION_1, SUITE_A, max_turns=3)
        assert len(recent) == 3
        # Should be the last 3
        assert recent[0].content == "msg-7"
        assert recent[2].content == "msg-9"

    @pytest.mark.asyncio
    async def test_clear_session(self, wm):
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="hello"))
        await wm.clear_session(SESSION_1, SUITE_A)

        turns = await wm.get_recent_turns(SESSION_1, SUITE_A)
        assert len(turns) == 0

    @pytest.mark.asyncio
    async def test_empty_session_returns_empty(self, wm):
        turns = await wm.get_recent_turns("nonexistent", SUITE_A)
        assert turns == []


# ---------------------------------------------------------------------------
# Max turn cap
# ---------------------------------------------------------------------------

class TestWorkingMemoryTurnCap:
    @pytest.mark.asyncio
    async def test_max_50_turns(self, wm):
        for i in range(60):
            await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content=f"msg-{i}"))

        all_turns = await wm.get_all_turns(SESSION_1, SUITE_A)
        assert len(all_turns) == 50
        # Should keep the LAST 50 (msg-10 through msg-59)
        assert all_turns[0].content == "msg-10"
        assert all_turns[-1].content == "msg-59"


# ---------------------------------------------------------------------------
# Tenant Isolation (Law #6)
# ---------------------------------------------------------------------------

class TestWorkingMemoryTenantIsolation:
    @pytest.mark.asyncio
    async def test_suites_isolated(self, wm):
        """Suite A cannot see Suite B's working memory."""
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="suite-a-data"))
        await wm.add_turn(SESSION_1, SUITE_B, ConversationTurn(role="user", content="suite-b-data"))

        a_turns = await wm.get_recent_turns(SESSION_1, SUITE_A)
        b_turns = await wm.get_recent_turns(SESSION_1, SUITE_B)

        assert len(a_turns) == 1
        assert a_turns[0].content == "suite-a-data"
        assert len(b_turns) == 1
        assert b_turns[0].content == "suite-b-data"

    @pytest.mark.asyncio
    async def test_sessions_isolated(self, wm):
        """Different sessions within same suite are isolated."""
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="sess-1"))
        await wm.add_turn(SESSION_2, SUITE_A, ConversationTurn(role="user", content="sess-2"))

        s1 = await wm.get_recent_turns(SESSION_1, SUITE_A)
        s2 = await wm.get_recent_turns(SESSION_2, SUITE_A)

        assert len(s1) == 1
        assert s1[0].content == "sess-1"
        assert len(s2) == 1
        assert s2[0].content == "sess-2"

    @pytest.mark.asyncio
    async def test_clear_only_affects_own_session(self, wm):
        """Clearing one session doesn't affect others."""
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="keep"))
        await wm.add_turn(SESSION_2, SUITE_A, ConversationTurn(role="user", content="clear"))

        await wm.clear_session(SESSION_2, SUITE_A)

        s1 = await wm.get_recent_turns(SESSION_1, SUITE_A)
        s2 = await wm.get_recent_turns(SESSION_2, SUITE_A)
        assert len(s1) == 1
        assert len(s2) == 0


# ---------------------------------------------------------------------------
# TTL Eviction
# ---------------------------------------------------------------------------

class TestWorkingMemoryTTL:
    @pytest.mark.asyncio
    async def test_expired_sessions_evicted(self, wm):
        """Expired sessions should be cleaned up on next access."""
        await wm.add_turn(SESSION_1, SUITE_A, ConversationTurn(role="user", content="old"))

        # Manually expire the session
        key = wm._make_key(SUITE_A, SESSION_1)
        wm._expiry[key] = time.monotonic() - 1  # Already expired

        turns = await wm.get_recent_turns(SESSION_1, SUITE_A)
        assert len(turns) == 0


# ---------------------------------------------------------------------------
# Key format
# ---------------------------------------------------------------------------

class TestWorkingMemoryKeys:
    def test_key_format_includes_suite_and_session(self, wm):
        key = wm._make_key("suite-123", "sess-456")
        assert key == "aspire:memory:working:suite-123:sess-456"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestWorkingMemorySingleton:
    def test_singleton(self):
        import aspire_orchestrator.services.working_memory as mod
        mod._memory = None
        m1 = get_working_memory()
        m2 = get_working_memory()
        assert m1 is m2
        mod._memory = None  # Cleanup
