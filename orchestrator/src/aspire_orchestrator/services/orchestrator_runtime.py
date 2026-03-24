"""Shared LangGraph runtime helpers for HTTP and Temporal execution paths."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langgraph.types import Command

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.graph import build_orchestrator_graph_async

logger = logging.getLogger(__name__)

_orchestrator_graph: Any | None = None
_checkpointer_failover_lock = asyncio.Lock()
_graph_init_lock = asyncio.Lock()
_checkpointer_force_memory = False


class GraphInvokeUnavailableError(RuntimeError):
    """Raised when graph invoke cannot run with the active checkpointer."""


def resolve_thread_id(
    request: dict[str, Any],
    *,
    suite_id: str | None,
    actor_id: str,
    correlation_id: str,
) -> str:
    """Derive a deterministic LangGraph thread id for a session/request."""
    payload = request.get("payload", {}) if isinstance(request.get("payload"), dict) else {}

    raw_session_id = (
        request.get("session_id")
        or request.get("conversation_id")
        or payload.get("session_id")
        or payload.get("conversation_id")
        or ""
    )
    session_id = str(raw_session_id).strip()

    raw_agent = (
        request.get("requested_agent")
        or request.get("agent")
        or payload.get("requested_agent")
        or payload.get("agent")
        or "ava"
    )
    agent_id = str(raw_agent).strip().lower() or "ava"
    safe_suite_id = (suite_id or "unknown").strip() or "unknown"
    safe_actor_id = actor_id.strip() or "unknown"

    if session_id:
        return f"{safe_suite_id}:{session_id}:{agent_id}"
    return f"{safe_suite_id}:{safe_actor_id}:{agent_id}:{correlation_id}"


async def warm_orchestrator_graph() -> None:
    """Ensure the compiled LangGraph runtime exists."""
    global _orchestrator_graph
    if _orchestrator_graph is not None:
        return
    async with _graph_init_lock:
        if _orchestrator_graph is None:
            _orchestrator_graph = await build_orchestrator_graph_async()


def _is_prepared_statement_error(err: Exception) -> bool:
    """Detect PgBouncer/psycopg prepared statement mismatch errors."""
    text = str(err).lower()
    return "prepared statement" in text and ("already exists" in text or "does not exist" in text)


async def _force_memory_checkpointer_graph(reason: Exception) -> bool:
    """Switch graph runtime to memory when Postgres checkpointing is unstable."""
    global _orchestrator_graph, _checkpointer_force_memory
    async with _checkpointer_failover_lock:
        if _checkpointer_force_memory:
            return True

        previous_mode = settings.langgraph_checkpointer
        previous_allow = os.environ.get("ASPIRE_ALLOW_MEMORY_CHECKPOINTER_IN_PROD")
        try:
            logger.error(
                "Detected unstable Postgres checkpointer, forcing MemorySaver failover: %s",
                reason,
            )
            os.environ["ASPIRE_ALLOW_MEMORY_CHECKPOINTER_IN_PROD"] = "1"
            settings.langgraph_checkpointer = "memory"
            _orchestrator_graph = await build_orchestrator_graph_async()
            _checkpointer_force_memory = True
            return True
        except Exception as failover_err:  # pragma: no cover - defensive path
            logger.exception("Failed to switch orchestrator graph to MemorySaver: %s", failover_err)
            return False
        finally:
            if not _checkpointer_force_memory:
                settings.langgraph_checkpointer = previous_mode
                if previous_allow is None:
                    os.environ.pop("ASPIRE_ALLOW_MEMORY_CHECKPOINTER_IN_PROD", None)
                else:
                    os.environ["ASPIRE_ALLOW_MEMORY_CHECKPOINTER_IN_PROD"] = previous_allow


# Graph execution timeout — prevents runaway LLM calls from blocking forever.
# Server proxy has 90s, this is the hard cap on the entire graph pipeline.
GRAPH_TIMEOUT_S = float(os.environ.get("ASPIRE_GRAPH_TIMEOUT_S", "75"))


async def invoke_orchestrator_graph(initial_state: Any, *, thread_id: str) -> dict[str, Any]:
    """Invoke orchestrator graph with async-first strategy, sync fallback, and timeout."""
    global _orchestrator_graph
    await warm_orchestrator_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        return await asyncio.wait_for(
            _orchestrator_graph.ainvoke(initial_state, config=config),
            timeout=GRAPH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error(
            "Graph execution timed out after %.0fs [thread_id=%s]",
            GRAPH_TIMEOUT_S,
            thread_id,
        )
        raise GraphInvokeUnavailableError(
            f"GRAPH_TIMEOUT: execution exceeded {GRAPH_TIMEOUT_S}s hard limit",
        )
    except NotImplementedError:
        logger.warning(
            "Async graph invoke unsupported by checkpointer; falling back to sync invoke [thread_id=%s]",
            thread_id,
        )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_orchestrator_graph.invoke, initial_state, config=config),
                timeout=GRAPH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error("Sync graph fallback timed out after %.0fs", GRAPH_TIMEOUT_S)
            raise GraphInvokeUnavailableError(
                f"GRAPH_TIMEOUT: sync fallback exceeded {GRAPH_TIMEOUT_S}s hard limit",
            )
        except Exception as sync_err:  # pragma: no cover - defensive path
            raise GraphInvokeUnavailableError(
                "CHECKPOINTER_UNAVAILABLE: async invoke unsupported and sync fallback failed",
            ) from sync_err
    except Exception as invoke_err:
        if _is_prepared_statement_error(invoke_err):
            switched = await _force_memory_checkpointer_graph(invoke_err)
            if switched and _orchestrator_graph is not None:
                return await asyncio.wait_for(
                    _orchestrator_graph.ainvoke(initial_state, config=config),
                    timeout=GRAPH_TIMEOUT_S,
                )
            raise GraphInvokeUnavailableError(
                "CHECKPOINTER_UNAVAILABLE: Postgres checkpointer failover to memory failed",
            ) from invoke_err
        raise


async def resume_orchestrator_graph(thread_id: str, resume_payload: dict[str, Any]) -> dict[str, Any]:
    """Resume a paused LangGraph thread after an approval or presence event."""
    return await invoke_orchestrator_graph(
        Command(resume=resume_payload),
        thread_id=thread_id,
    )

