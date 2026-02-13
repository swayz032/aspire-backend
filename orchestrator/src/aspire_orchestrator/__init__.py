"""Aspire LangGraph Orchestrator — the Single Brain (Law #1).

This is the only decision authority in the Aspire system.
All intents flow through: Intake → Safety → Policy → Approval → TokenMint → Execute → ReceiptWrite → Respond

No other component (n8n, MCP tools, UI, workers) is allowed to decide or execute autonomously.
"""

__version__ = "0.1.0"
