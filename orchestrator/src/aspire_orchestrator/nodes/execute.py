"""Execute Node — Bounded tool execution (Law #7) with A2A dispatch.

Responsibilities:
1. Execute the approved action via the appropriate skill pack
2. Tools are hands — they execute bounded commands, never decide
3. Validate capability token before execution
4. Handle execution failures with receipts
5. Set outcome (success/failed/timeout)
6. Enforce idempotency on state-changing operations (Phase 3 W5)
7. Route RED-tier ops through outbox for durable execution (Phase 3 W5)
8. Dispatch all executions through A2A for agent identity tracking

A2A Integration (Phase 3):
  - Every execution is dispatched via A2A to the owning agent
  - Ava orchestrates → A2A dispatch → Agent claims → Tool executes → Agent completes
  - Receipt trail preserves agent identity (who did what)
  - Phase 1: Synchronous dispatch/claim/execute/complete in same request cycle
  - Phase 2+: Async dispatch with agent workers claiming from queue
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import (
    AspireErrorCode,
    Outcome,
    ReceiptType,
)
from aspire_orchestrator.services.a2a_service import get_a2a_service
from aspire_orchestrator.services.idempotency_service import get_idempotency_service
from aspire_orchestrator.services.outbox_client import OutboxJob, get_outbox_client
from aspire_orchestrator.services.token_service import validate_token
from aspire_orchestrator.services.tool_executor import is_live_tool
from aspire_orchestrator.services.tool_executor import execute_tool as _execute_tool_async
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _resolve_agent_from_routing(state: OrchestratorState) -> str:
    """Extract owning agent from routing plan.

    The routing plan is set by route_node from SkillRouter.
    Each step has a skill_pack ID that maps to a manifest with an owner.
    Falls back to 'ava' if routing plan isn't available.
    """
    routing_plan = state.get("routing_plan")
    if not routing_plan or not isinstance(routing_plan, dict):
        return "ava"

    steps = routing_plan.get("steps", [])
    if not steps:
        return "ava"

    # Use the first step's skill_pack to determine agent
    skill_pack_id = steps[0].get("skill_pack", "")

    # skill_pack_id → agent owner mapping (from manifests)
    _PACK_TO_AGENT: dict[str, str] = {
        "sarah_front_desk": "sarah",
        "eli_inbox": "eli",
        "quinn_invoicing": "quinn",
        "nora_conference": "nora",
        "adam_research": "adam",
        "tec_documents": "tec",
        "finn_finance_manager": "finn",
        "milo_payroll": "milo",
        "teressa_books": "teressa",
        "clara_legal": "clara",
        "mail_ops_desk": "mail_ops",
    }

    return _PACK_TO_AGENT.get(skill_pack_id, "ava")


def _resolve_risk_tier(state: OrchestratorState) -> str:
    """Extract risk_tier as a string, handling both enum and str values."""
    risk_tier_val = state.get("risk_tier")
    return risk_tier_val.value if hasattr(risk_tier_val, "value") else str(risk_tier_val or "green")


async def execute_node(state: OrchestratorState) -> dict[str, Any]:
    """Execute the approved action via A2A dispatch to owning agent.

    Flow (Phase 1 — synchronous):
      1. Resolve target agent from routing plan (Quinn, Finn, etc.)
      2. Dispatch task via A2A (Ava → agent)
      3. Agent claims the task
      4. Validate capability token (Law #5)
      5. Execute tool (Law #7 — tools are hands)
      6. Agent completes task via A2A
      7. Receipt trail: a2a.dispatch → a2a.claim → tool_execution → a2a.complete
    """
    if state.get("error_code"):
        return {
            "outcome": Outcome.DENIED,
            "execution_result": None,
        }

    # Defense-in-depth: check safe mode before any tool execution (Law #3)
    try:
        from aspire_orchestrator.config.settings import settings as _settings
        if _settings.ava_safe_mode:
            logger.warning("Execute denied: safe mode active")
            return {
                "outcome": Outcome.DENIED,
                "execution_result": {"status": "denied", "reason": "SAFE_MODE", "stub": False},
                "error_code": "SAFE_MODE",
                "error_message": "Safe mode active — all tool execution disabled",
            }
    except Exception:
        pass  # Settings not available — proceed (non-critical)

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    task_type = state.get("task_type", "unknown")
    allowed_tools = state.get("allowed_tools", [])
    capability_token_id = state.get("capability_token_id")
    capability_token = state.get("capability_token")
    risk_tier_str = _resolve_risk_tier(state)

    # Resolve which agent owns this execution
    assigned_agent = _resolve_agent_from_routing(state)

    # -------------------------------------------------------------------
    # Capability token validation — full 6-check (Law #3 + Law #5)
    # This is the enforcement boundary: tokens are minted by token_mint
    # node, but MUST be validated again here before any execution.
    # -------------------------------------------------------------------
    def _deny_execution(reason_code: str, message: str) -> dict[str, Any]:
        """Build denial response with receipt. Agent identity preserved."""
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "agent",
            "actor_id": assigned_agent,
            "action_type": f"execute.{task_type}",
            "risk_tier": risk_tier_str,
            "tool_used": allowed_tools[0] if allowed_tools else "unknown",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": Outcome.DENIED.value,
            "reason_code": reason_code,
            "receipt_type": ReceiptType.TOOL_EXECUTION.value,
            "receipt_hash": "",
        }
        existing = list(state.get("pipeline_receipts", []))
        existing.append(receipt)
        return {
            "outcome": Outcome.DENIED,
            "execution_result": None,
            "error_code": reason_code,
            "error_message": message,
            "tool_used": allowed_tools[0] if allowed_tools else "unknown",
            "pipeline_receipts": existing,
            "assigned_agent": assigned_agent,
        }

    # Check 0: Token must exist
    if not capability_token_id or not capability_token:
        logger.warning(
            "Execution denied: missing capability token for %s (suite=%s)",
            task_type, suite_id[:8] if len(suite_id) > 8 else suite_id,
        )
        return _deny_execution(
            AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value,
            "Capability token required for execution",
        )

    # Full 6-check validation (signature, expiry, revocation, scope, suite, office)
    tool_used = allowed_tools[0] if allowed_tools else "unknown"
    verb = task_type.split(".")[-1] if "." in task_type else task_type
    scope_map = {
        "read": "read", "list": "read", "search": "read",
        "create": "write", "send": "write", "draft": "write",
        "schedule": "write", "sign": "write", "transfer": "write",
        "delete": "delete", "purchase": "write",
    }
    scope_verb = scope_map.get(verb, "execute")
    domain = task_type.split(".")[0] if "." in task_type else task_type
    required_scope = f"{domain}.{scope_verb}"

    validation = validate_token(
        capability_token,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope=required_scope,
    )

    if not validation.valid:
        logger.warning(
            "Execution denied: token validation failed (%s) for %s (suite=%s), checks_passed=%d/6",
            validation.error.value if validation.error else "unknown",
            task_type, suite_id[:8] if len(suite_id) > 8 else suite_id,
            validation.checks_passed,
        )
        return _deny_execution(
            AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value,
            f"Token validation failed: {validation.error_message}",
        )

    # -------------------------------------------------------------------
    # Idempotency enforcement (Law #3 — fail-closed on duplicates)
    # State-changing operations (YELLOW/RED) get idempotency checks.
    # GREEN read-only ops skip idempotency (no side effects).
    # -------------------------------------------------------------------
    idempotency_key = state.get("idempotency_key")
    is_state_changing = risk_tier_str in ("yellow", "red")

    if is_state_changing and idempotency_key:
        idem_svc = get_idempotency_service()
        idem_result = idem_svc.check_and_reserve(
            suite_id=suite_id,
            idempotency_key=idempotency_key,
            action_type=task_type,
        )

        if not idem_result.should_execute:
            logger.warning(
                "Idempotency key already used: key=%s suite=%s action=%s original_receipt=%s",
                idempotency_key[:8], suite_id[:8] if len(suite_id) > 8 else suite_id,
                task_type, idem_result.original_receipt_id,
            )
            # Build idempotency-rejection receipt (Law #2)
            idem_receipt = {
                "id": str(uuid.uuid4()),
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "office_id": office_id,
                "actor_type": "system",
                "actor_id": "executor",
                "action_type": f"execute.{task_type}",
                "risk_tier": risk_tier_str,
                "tool_used": allowed_tools[0] if allowed_tools else "unknown",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "outcome": Outcome.DENIED.value,
                "reason_code": "IDEMPOTENCY_DUPLICATE",
                "receipt_type": ReceiptType.TOOL_EXECUTION.value,
                "receipt_hash": "",
                "idempotency_key": idempotency_key,
                "original_receipt_id": idem_result.original_receipt_id,
            }
            existing = list(state.get("pipeline_receipts", []))
            existing.append(idem_receipt)
            return {
                "outcome": Outcome.DENIED,
                "execution_result": None,
                "error_code": "IDEMPOTENCY_DUPLICATE",
                "error_message": "Operation already executed with this idempotency key",
                "tool_used": allowed_tools[0] if allowed_tools else "unknown",
                "pipeline_receipts": existing,
                "original_receipt_id": idem_result.original_receipt_id,
            }

    # Resolve tool to execute (tool_used already set during scope derivation)
    live = is_live_tool(tool_used)

    logger.info(
        "Executing tool: %s (live=%s, tier=%s) for task=%s, agent=%s, suite=%s",
        tool_used, live, risk_tier_str, task_type, assigned_agent,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    # -------------------------------------------------------------------
    # A2A Dispatch — Ava delegates to owning agent (Law #1 + Law #2)
    # This creates the agent identity chain: Ava → dispatch → Agent → execute
    # Phase 1: synchronous dispatch/claim in same request cycle.
    # -------------------------------------------------------------------
    a2a = get_a2a_service()
    existing_receipts = list(state.get("pipeline_receipts", []))

    dispatch_result = a2a.dispatch(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        task_type=task_type,
        assigned_to_agent=assigned_agent,
        payload={
            "tool_used": tool_used,
            "risk_tier": risk_tier_str,
            "capability_token_id": capability_token_id,
            "live": live,
        },
        priority=1 if risk_tier_str == "red" else (2 if risk_tier_str == "yellow" else 3),
        idempotency_key=idempotency_key,
        actor_id="ava",
    )

    if not dispatch_result.success:
        logger.error("A2A dispatch failed: %s", dispatch_result.error)
        return _deny_execution("A2A_DISPATCH_FAILED", f"A2A dispatch failed: {dispatch_result.error}")

    a2a_task_id = dispatch_result.task_id
    if dispatch_result.receipt_data:
        existing_receipts.append(dispatch_result.receipt_data)

    # Agent claims the task (synchronous in Phase 1)
    claim_result = a2a.claim(
        agent_id=assigned_agent,
        suite_id=suite_id,
        task_types=[task_type],
    )

    if claim_result.success and claim_result.receipt_data:
        existing_receipts.append(claim_result.receipt_data)

    logger.info(
        "A2A: %s dispatched to %s, task_id=%s",
        task_type, assigned_agent,
        (a2a_task_id or "?")[:8],
    )

    # -------------------------------------------------------------------
    # RED-tier operations → Outbox for durable execution (Law #4)
    # RED ops are too risky for synchronous execution: if the orchestrator
    # crashes mid-execution, the outbox ensures retry/completion.
    # -------------------------------------------------------------------
    if risk_tier_str == "red":
        outbox = get_outbox_client()
        job = OutboxJob(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=task_type,
            risk_tier="red",
            payload={
                "tool_used": tool_used,
                "capability_token_id": capability_token_id,
                "live": live,
                "a2a_task_id": a2a_task_id,
                "assigned_agent": assigned_agent,
            },
            idempotency_key=idempotency_key,
            capability_token_id=capability_token_id,
        )

        # Outbox submission receipt (Law #2)
        outbox_receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "agent",
            "actor_id": assigned_agent,
            "action_type": f"execute.{task_type}",
            "risk_tier": "red",
            "tool_used": tool_used,
            "capability_token_id": capability_token_id,
            "capability_token_hash": state.get("capability_token_hash"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": Outcome.SUCCESS.value,
            "reason_code": "OUTBOX_SUBMITTED",
            "receipt_type": ReceiptType.TOOL_EXECUTION.value,
            "receipt_hash": "",
            "outbox_job_id": job.job_id,
            "a2a_task_id": a2a_task_id,
            "idempotency_key": idempotency_key,
        }
        existing_receipts.append(outbox_receipt)

        # Mark idempotency as completed with the outbox receipt
        if is_state_changing and idempotency_key:
            idem_svc = get_idempotency_service()
            idem_svc.mark_completed(
                suite_id=suite_id,
                idempotency_key=idempotency_key,
                receipt_id=outbox_receipt["id"],
            )

        logger.info(
            "RED-tier op routed to outbox: job_id=%s action=%s agent=%s suite=%s",
            job.job_id[:8], task_type, assigned_agent,
            suite_id[:8] if len(suite_id) > 8 else suite_id,
        )

        return {
            "outcome": Outcome.SUCCESS,
            "execution_result": {
                "status": "outbox_submitted",
                "tool": tool_used,
                "task_type": task_type,
                "outbox_job_id": job.job_id,
                "a2a_task_id": a2a_task_id,
                "assigned_agent": assigned_agent,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "stub": not live,
                "live": live,
            },
            "tool_used": tool_used,
            "assigned_agent": assigned_agent,
            "pipeline_receipts": existing_receipts,
        }

    # -------------------------------------------------------------------
    # GREEN/YELLOW ops → Real execution via execute_tool
    # -------------------------------------------------------------------
    execution_result: dict[str, Any]

    if live and state.get("execution_params"):
        try:
            tool_result = await _execute_tool_async(
                tool_id=tool_used,
                payload=state["execution_params"],
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                risk_tier=risk_tier_str,
                capability_token_id=capability_token_id,
            )
            # ToolExecutionResult uses .outcome (Outcome enum), not .success
            execution_success = (
                tool_result.outcome == Outcome.SUCCESS
                if hasattr(tool_result, "outcome")
                else bool(tool_result)
            )
            execution_data = tool_result.data if hasattr(tool_result, "data") else {}
            execution_error = tool_result.error if hasattr(tool_result, "error") else None

            execution_result = {
                "status": "success" if execution_success else "failed",
                "tool": tool_used,
                "task_type": task_type,
                "assigned_agent": assigned_agent,
                "a2a_task_id": a2a_task_id,
                "data": execution_data if isinstance(execution_data, dict) else {},
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "stub": False,
                "live": True,
            }
            if execution_error:
                execution_result["error"] = execution_error
        except Exception as e:
            logger.error("Live tool execution failed for %s: %s", tool_used, e)
            execution_result = {
                "status": "failed",
                "tool": tool_used,
                "task_type": task_type,
                "assigned_agent": assigned_agent,
                "a2a_task_id": a2a_task_id,
                "error": str(e),
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "stub": False,
                "live": True,
            }
    else:
        # Stub path (for tools not yet in live executors, or no execution_params)
        execution_result = {
            "status": "success",
            "tool": tool_used,
            "task_type": task_type,
            "assigned_agent": assigned_agent,
            "a2a_task_id": a2a_task_id,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stub": not live,
            "live": live,
        }

    receipt_id = str(uuid.uuid4())
    outcome_val = Outcome.SUCCESS if execution_result["status"] == "success" else Outcome.FAILED

    # Tool execution receipt — agent identity preserved
    receipt = {
        "id": receipt_id,
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "agent",
        "actor_id": assigned_agent,
        "action_type": f"execute.{task_type}",
        "risk_tier": risk_tier_str,
        "tool_used": tool_used,
        "capability_token_id": capability_token_id,
        "capability_token_hash": state.get("capability_token_hash"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome_val.value,
        "reason_code": "EXECUTED" if execution_result["status"] == "success" else "EXECUTION_FAILED",
        "receipt_type": ReceiptType.TOOL_EXECUTION.value,
        "receipt_hash": "",
        "a2a_task_id": a2a_task_id,
    }
    existing_receipts.append(receipt)

    # Mark idempotency as completed for YELLOW ops
    if is_state_changing and idempotency_key:
        idem_svc = get_idempotency_service()
        idem_svc.mark_completed(
            suite_id=suite_id,
            idempotency_key=idempotency_key,
            receipt_id=receipt_id,
        )

    # -------------------------------------------------------------------
    # Post-execute: Queue invoice.send in Authority Queue (draft-first pattern)
    #
    # When invoice.create succeeds (GREEN), we immediately queue invoice.send
    # (YELLOW) so the user can approve sending from the Authority Queue.
    # -------------------------------------------------------------------
    authority_queue_id = None
    if (
        task_type == "invoice.create"
        and outcome_val == Outcome.SUCCESS
        and execution_result.get("data", {}).get("invoice_id")
    ):
        try:
            import hashlib
            import json
            from datetime import timedelta

            from aspire_orchestrator.services.supabase_client import supabase_insert

            invoice_data = execution_result.get("data", {})
            exec_params = state.get("execution_params") or {}
            invoice_id = invoice_data["invoice_id"]
            customer_name = exec_params.get("customer_name") or exec_params.get("client_name") or "client"
            amount_cents = exec_params.get("amount_cents") or exec_params.get("amount") or invoice_data.get("amount_due", 0)
            currency = exec_params.get("currency", "usd").upper()

            # Build send params for resume execution
            send_params = {
                "invoice_id": invoice_id,
                "customer_name": customer_name,
                "customer_email": exec_params.get("customer_email", ""),
                "amount_cents": int(amount_cents) if amount_cents else 0,
                "currency": currency,
                "description": exec_params.get("description", ""),
            }

            amount_display = f"${int(amount_cents) / 100:.2f}" if amount_cents else ""
            draft_summary = f"Send invoice to {customer_name}" + (f" — {amount_display}" if amount_display else "")

            params_hash = hashlib.sha256(
                json.dumps(send_params, sort_keys=True, default=str).encode()
            ).hexdigest()

            from aspire_orchestrator.services.approval_service import compute_payload_hash

            approval_hash = compute_payload_hash({
                "task_type": "invoice.send",
                "parameters": send_params,
                "suite_id": suite_id,
                "office_id": office_id,
            })

            # created_by_user_id is UUID-typed — only pass a valid UUID or None
            actor_id_raw = state.get("actor_id")
            created_by: str | None = None
            if actor_id_raw and actor_id_raw != "unknown":
                try:
                    uuid.UUID(actor_id_raw)  # Validate it's a real UUID
                    created_by = actor_id_raw
                except (ValueError, AttributeError):
                    created_by = None

            send_approval_data = {
                "approval_id": str(uuid.uuid4()),
                "tenant_id": suite_id,
                "run_id": correlation_id,
                "tool": "stripe.invoice.send",
                "operation": "invoice.send",
                "risk_tier": "yellow",
                "policy_version": "v1",
                "approval_hash": approval_hash,
                "status": "pending",
                "created_by_user_id": created_by,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "execution_payload": send_params,
                "draft_summary": draft_summary,
                "assigned_agent": assigned_agent,
                "execution_params_hash": params_hash,
                "payload_redacted": {
                    "invoice_id": invoice_id,
                    "customer_name": customer_name,
                    "amount_cents": int(amount_cents) if amount_cents else 0,
                    "currency": currency,
                },
            }

            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    aq_result = pool.submit(
                        asyncio.run, supabase_insert("approval_requests", send_approval_data)
                    ).result(timeout=8)
            else:
                aq_result = asyncio.run(supabase_insert("approval_requests", send_approval_data))

            authority_queue_id = aq_result.get("approval_id") or aq_result.get("id")
            execution_result["authority_queue_id"] = authority_queue_id
            logger.info(
                "invoice.send queued in Authority Queue: aq_id=%s invoice=%s suite=%s",
                authority_queue_id, invoice_id[:12] if invoice_id else "?",
                suite_id[:8] if len(suite_id) > 8 else suite_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to queue invoice.send in Authority Queue: %s (suite=%s)",
                e, suite_id[:8] if len(suite_id) > 8 else suite_id,
            )
            # Non-fatal: invoice draft was created successfully, queue failure doesn't block

    # -------------------------------------------------------------------
    # A2A Complete — Agent reports task done (Law #2)
    # -------------------------------------------------------------------
    if a2a_task_id:
        complete_result = a2a.complete(
            task_id=a2a_task_id,
            agent_id=assigned_agent,
            suite_id=suite_id,
            result={
                "tool_used": tool_used,
                "receipt_id": receipt_id,
                "outcome": outcome_val.value,
            },
        )
        if complete_result.success and complete_result.receipt_data:
            existing_receipts.append(complete_result.receipt_data)

    return {
        "outcome": outcome_val,
        "execution_result": execution_result,
        "tool_used": tool_used,
        "assigned_agent": assigned_agent,
        "pipeline_receipts": existing_receipts,
    }
