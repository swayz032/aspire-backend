"""Temporal integration for Aspire — durable workflow orchestration layer.

Temporal owns long-running jobs, approval waits, retries, fan-out/fan-in.
LangGraph owns reasoning and intent processing (invoked as Temporal activities).

Architecture boundary (ADR-001):
  Temporal = outer orchestration, LangGraph = inner reasoning.
"""
