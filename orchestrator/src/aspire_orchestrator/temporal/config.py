"""Temporal configuration — namespaces, task queues, timeouts, ID contracts.

Enhancement #5: Workflow IDs include random suffix to prevent cross-tenant prediction.
Enhancement #9: Custom search attributes for admin visibility.
Enhancement #12: 4-queue priority-based worker topology.
"""

from __future__ import annotations

import os
import secrets
from typing import Final

# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------
NAMESPACE_PRODUCTION: Final[str] = "aspire-production"
NAMESPACE_STAGING: Final[str] = "aspire-staging"
NAMESPACE_DEV: Final[str] = "aspire-dev"


def get_namespace() -> str:
    """Return namespace based on ASPIRE_ENV (default: dev)."""
    env = os.getenv("ASPIRE_ENV", "dev").lower()
    return {
        "production": NAMESPACE_PRODUCTION,
        "staging": NAMESPACE_STAGING,
    }.get(env, NAMESPACE_DEV)


# ---------------------------------------------------------------------------
# Task Queues — Enhancement #12: Priority-Based Worker Topology
# ---------------------------------------------------------------------------
# SLA: P50 < 500ms — user-facing intents, approval updates
QUEUE_INTENT_HIGH: Final[str] = "ava-intent-high"
# SLA: P50 < 30s — agent fan-out, outbox execution
QUEUE_BACKGROUND: Final[str] = "ava-background"
# SLA: P50 < 5s — webhook signal routing, async completions
QUEUE_CALLBACKS: Final[str] = "ava-callbacks"
# SLA: best-effort — reminders, SLA checks, reconciliation
QUEUE_SCHEDULED: Final[str] = "ava-scheduled"

ALL_QUEUES: Final[list[str]] = [
    QUEUE_INTENT_HIGH,
    QUEUE_BACKGROUND,
    QUEUE_CALLBACKS,
    QUEUE_SCHEDULED,
]

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------
ACTIVITY_START_TO_CLOSE_DEFAULT: Final[int] = 30
ACTIVITY_HEARTBEAT_DEFAULT: Final[int] = 15
INTENT_WORKFLOW_TIMEOUT: Final[int] = 120  # 2 minutes for full intent cycle
APPROVAL_DEFAULT_TIMEOUT_HOURS: Final[int] = 24
CALLBACK_DEFAULT_TIMEOUT_HOURS: Final[int] = 72
FANOUT_SLA_TIMEOUT_MINUTES: Final[int] = 10

# Continue-as-new threshold (Enhancement #7)
CONTINUE_AS_NEW_EVENT_THRESHOLD: Final[int] = 10_000

# ---------------------------------------------------------------------------
# Search Attributes — Enhancement #9
# ---------------------------------------------------------------------------
SEARCH_ATTR_SUITE_ID: Final[str] = "suite_id"
SEARCH_ATTR_RISK_TIER: Final[str] = "risk_tier"
SEARCH_ATTR_AGENT_ID: Final[str] = "agent_id"
SEARCH_ATTR_WORKFLOW_KIND: Final[str] = "workflow_kind"
SEARCH_ATTR_OFFICE_ID: Final[str] = "office_id"
SEARCH_ATTR_CORRELATION_ID: Final[str] = "correlation_id"

SEARCH_ATTRIBUTES: Final[dict[str, str]] = {
    SEARCH_ATTR_SUITE_ID: "Keyword",
    SEARCH_ATTR_RISK_TIER: "Keyword",
    SEARCH_ATTR_AGENT_ID: "Keyword",
    SEARCH_ATTR_WORKFLOW_KIND: "Keyword",
    SEARCH_ATTR_OFFICE_ID: "Keyword",
    SEARCH_ATTR_CORRELATION_ID: "Keyword",
}

# ---------------------------------------------------------------------------
# Workflow ID Contract — Enhancement #5: Random suffix prevents prediction
# ---------------------------------------------------------------------------
_RANDOM_SUFFIX_LENGTH: Final[int] = 8


def _rand_suffix() -> str:
    return secrets.token_urlsafe(_RANDOM_SUFFIX_LENGTH)[:_RANDOM_SUFFIX_LENGTH]


def workflow_id_intent(suite_id: str, correlation_id: str) -> str:
    return f"suite:{suite_id}:intent:{correlation_id}:{_rand_suffix()}"


def workflow_id_approval(suite_id: str, approval_id: str) -> str:
    return f"suite:{suite_id}:approval:{approval_id}:{_rand_suffix()}"


def workflow_id_outbox(suite_id: str, job_id: str) -> str:
    return f"suite:{suite_id}:outbox:{job_id}:{_rand_suffix()}"


def workflow_id_callback(suite_id: str, provider: str, ref_id: str) -> str:
    return f"suite:{suite_id}:callback:{provider}:{ref_id}:{_rand_suffix()}"


def workflow_id_fanout(suite_id: str, correlation_id: str) -> str:
    return f"suite:{suite_id}:fanout:{correlation_id}:{_rand_suffix()}"


def workflow_id_agent(suite_id: str, agent_id: str, correlation_id: str) -> str:
    return f"suite:{suite_id}:agent:{agent_id}:{correlation_id}:{_rand_suffix()}"


def extract_suite_id_from_workflow_id(workflow_id: str) -> str | None:
    """Extract suite_id from a workflow ID for cross-tenant validation (Enhancement #5)."""
    parts = workflow_id.split(":")
    if len(parts) >= 2 and parts[0] == "suite":
        return parts[1]
    return None


# ---------------------------------------------------------------------------
# Feature Flags
# ---------------------------------------------------------------------------
def temporal_intent_enabled() -> bool:
    return os.getenv("TEMPORAL_INTENT_ENABLED", "false").lower() == "true"


def temporal_approval_enabled() -> bool:
    return os.getenv("TEMPORAL_APPROVAL_ENABLED", "false").lower() == "true"


def temporal_outbox_enabled() -> bool:
    return os.getenv("TEMPORAL_OUTBOX_ENABLED", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def get_temporal_target() -> str:
    """Return Temporal server address (gRPC)."""
    return os.getenv("TEMPORAL_TARGET", "localhost:7233")


def get_kms_key_arn() -> str | None:
    """Return AWS KMS key ARN for PayloadCodec (Enhancement #6). None = dev (no encryption)."""
    return os.getenv("TEMPORAL_KMS_KEY_ARN")
