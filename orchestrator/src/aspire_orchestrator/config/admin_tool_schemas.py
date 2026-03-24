"""OpenAI function/tool definitions for Admin Ava's 24 capabilities.

Maps 1:1 to AvaAdminSkillPack methods in skillpacks/ava_admin.py.
Used by the LangGraph orchestrator to expose admin tools to GPT.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tool definitions — OpenAI function-calling format
# ---------------------------------------------------------------------------

# --- Platform Health & Incidents ---

_HEALTH_PULSE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_health_pulse",
        "description": "Get a real-time platform health pulse across all services.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

_TRIAGE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_triage",
        "description": "Triage a specific incident by ID with root-cause analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "The incident ID to triage.",
                },
            },
            "required": ["incident_id"],
        },
    },
}

_LIST_INCIDENTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_list_incidents",
        "description": "List incidents filtered by state and severity.",
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Filter by incident state (e.g. open, resolved).",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter by severity (e.g. critical, high, medium, low).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max incidents to return. Default 20.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

_TRACE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_trace",
        "description": "Look up a full execution trace by correlation ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "correlation_id": {
                    "type": "string",
                    "description": "The correlation ID to trace.",
                },
            },
            "required": ["correlation_id"],
        },
    },
}

_METRICS_SNAPSHOT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_metrics_snapshot",
        "description": "Get a snapshot of key platform metrics and KPIs.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# --- Provider & Error Analysis ---

_PROVIDER_ANALYSIS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_provider_analysis",
        "description": "Analyze provider errors with optional provider filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Filter to a specific provider (e.g. openai, stripe).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max error records to analyze. Default 100.",
                    "default": 100,
                },
            },
            "required": [],
        },
    },
}

_PROVIDER_CALL_LOGS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_provider_call_logs",
        "description": "Fetch provider call logs filtered by provider and status.",
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Filter to a specific provider.",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by call status (e.g. success, error).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max log entries to return. Default 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
}

_WEBHOOK_HEALTH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_webhook_health",
        "description": "Check webhook delivery health, optionally for one provider.",
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Filter to a specific webhook provider.",
                },
            },
            "required": [],
        },
    },
}

# --- Sentry & Error Monitoring ---

_SENTRY_SUMMARY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_sentry_summary",
        "description": "Get a summary of Sentry errors across all projects.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

_SENTRY_ISSUES: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_sentry_issues",
        "description": "List Sentry issues, optionally filtered by project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Sentry project slug to filter by.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return. Default 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
}

_CLIENT_EVENTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_client_events",
        "description": "Fetch client-side events filtered by type and severity.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "Filter by event type.",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter by severity level.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return. Default 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
}

# --- Robots & Council ---

_ROBOT_TRIAGE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_robot_triage",
        "description": "Triage a robot run failure by run ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The robot run ID to investigate.",
                },
            },
            "required": ["run_id"],
        },
    },
}

_COUNCIL_DISPATCH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_council_dispatch",
        "description": "Dispatch a Meeting of Minds council for an incident.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "The incident ID to convene the council for.",
                },
            },
            "required": ["incident_id"],
        },
    },
}

_COUNCIL_HISTORY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_council_history",
        "description": "View past council sessions with optional status filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by council status.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to return. Default 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
}

_LEARNING_ENTRY_CREATE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_learning_entry_create",
        "description": "Log a learning entry (lesson learned) for an incident.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "The incident this lesson relates to.",
                },
                "lesson": {
                    "type": "string",
                    "description": "The lesson learned content.",
                },
            },
            "required": ["incident_id", "lesson"],
        },
    },
}

# --- Workflows & Operations ---

_WORKFLOW_STATUS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_workflow_status",
        "description": "Get status of n8n workflows.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max workflows to return. Default 20.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

_N8N_OPERATIONS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_n8n_operations",
        "description": "Fetch recent n8n workflow execution operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max operations to return. Default 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
}

_OUTBOX_STATUS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_outbox_status",
        "description": "Check the transactional outbox queue status.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max outbox entries to return. Default 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
}

# --- Governance & Receipts ---

_APPROVAL_QUEUE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_approval_queue",
        "description": "View the approval queue filtered by status.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by approval status. Default 'pending'.",
                    "default": "pending",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return. Default 20.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

_RECEIPT_AUDIT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_receipt_audit",
        "description": "Audit receipt chain integrity for a suite.",
        "parameters": {
            "type": "object",
            "properties": {
                "suite_id": {
                    "type": "string",
                    "description": "Suite to audit. Default 'system'.",
                    "default": "system",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max receipts to audit. Default 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
}

_MODEL_POLICY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_model_policy",
        "description": "View the current LLM model routing policy.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# --- Database & Infrastructure ---

_DB_PERFORMANCE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_db_performance",
        "description": "Get database performance stats (slow queries, connections).",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# --- Research & Business ---

_WEB_SEARCH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_web_search",
        "description": "Run a web search query via Exa.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results. Default 5.",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

_BUSINESS_SNAPSHOT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "admin_ops_business_snapshot",
        "description": "Get a business metrics snapshot (revenue, users, growth).",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max data points to include. Default 100.",
                    "default": 100,
                },
            },
            "required": [],
        },
    },
}

# ---------------------------------------------------------------------------
# Complete tool list — all 24 capabilities
# ---------------------------------------------------------------------------

ADMIN_AVA_TOOLS: list[dict[str, Any]] = [
    # Platform Health & Incidents
    _HEALTH_PULSE,
    _TRIAGE,
    _LIST_INCIDENTS,
    _TRACE,
    _METRICS_SNAPSHOT,
    # Provider & Error Analysis
    _PROVIDER_ANALYSIS,
    _PROVIDER_CALL_LOGS,
    _WEBHOOK_HEALTH,
    # Sentry & Error Monitoring
    _SENTRY_SUMMARY,
    _SENTRY_ISSUES,
    _CLIENT_EVENTS,
    # Robots & Council
    _ROBOT_TRIAGE,
    _COUNCIL_DISPATCH,
    _COUNCIL_HISTORY,
    _LEARNING_ENTRY_CREATE,
    # Workflows & Operations
    _WORKFLOW_STATUS,
    _N8N_OPERATIONS,
    _OUTBOX_STATUS,
    # Governance & Receipts
    _APPROVAL_QUEUE,
    _RECEIPT_AUDIT,
    _MODEL_POLICY,
    # Database & Infrastructure
    _DB_PERFORMANCE,
    # Research & Business
    _WEB_SEARCH,
    _BUSINESS_SNAPSHOT,
]

# ---------------------------------------------------------------------------
# Tool name → skill pack method name mapping
# (Names are identical — kept explicit for routing clarity)
# ---------------------------------------------------------------------------

TOOL_NAME_TO_METHOD: dict[str, str] = {
    tool["function"]["name"]: tool["function"]["name"]
    for tool in ADMIN_AVA_TOOLS
}

# ---------------------------------------------------------------------------
# Voice-channel reduced set (top 10 most useful for voice interaction)
# Prioritizes quick-answer, high-signal tools that work well spoken aloud.
# ---------------------------------------------------------------------------

_VOICE_TOOL_NAMES: set[str] = {
    "admin_ops_health_pulse",
    "admin_ops_triage",
    "admin_ops_list_incidents",
    "admin_ops_metrics_snapshot",
    "admin_ops_approval_queue",
    "admin_ops_sentry_summary",
    "admin_ops_workflow_status",
    "admin_ops_business_snapshot",
    "admin_ops_provider_analysis",
    "admin_ops_web_search",
}


def get_admin_tools(channel: str = "chat") -> list[dict[str, Any]]:
    """Return admin tool definitions scoped by interaction channel.

    Args:
        channel: ``"chat"`` returns all 24 tools. ``"voice"`` returns a
            reduced set of 10 high-signal tools to minimize token overhead
            during voice interactions.

    Returns:
        List of OpenAI function-calling tool definitions.
    """
    if channel == "voice":
        return [t for t in ADMIN_AVA_TOOLS if t["function"]["name"] in _VOICE_TOOL_NAMES]
    return list(ADMIN_AVA_TOOLS)
