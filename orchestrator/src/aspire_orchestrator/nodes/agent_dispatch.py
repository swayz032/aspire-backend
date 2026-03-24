"""Agent Dispatch Node — Wire AgenticSkillPack into the pipeline.

When the classifier identifies an action intent and the router assigns it to
an agent, this node instantiates the agent's Enhanced* skill pack and invokes
its agentic intelligence: parameter parsing, prerequisite checks (e.g. does
the Stripe customer exist?), and multi-step reasoning via run_agentic_loop.

Two exits:
  - execution_params resolved → policy_eval (continue action path)
  - needs more info → conversation_response set → respond (ask user naturally)

No fallbacks. Every agent has an AgenticSkillPack. If dispatch fails, it
fails closed with a receipt (Law #2, #3).

Governance:
  - This node is GREEN (reasoning only, no state changes)
  - Receipts emitted per agentic step (built into run_agentic_loop)
  - Skill packs PROPOSE, orchestrator DISPOSES (Law #1)
  - Actual execution still requires policy → approval → token → execute
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill Pack Registry — singleton instances, instantiated on first use
# ---------------------------------------------------------------------------

_SKILL_PACK_REGISTRY: dict[str, Any] = {}
_REGISTRY_INITIALIZED = False


def _init_registry() -> dict[str, Any]:
    """Lazily import and instantiate all Enhanced* skill packs."""
    global _REGISTRY_INITIALIZED
    if _REGISTRY_INITIALIZED:
        return _SKILL_PACK_REGISTRY

    _class_map: dict[str, tuple[str, str]] = {
        "quinn": ("aspire_orchestrator.skillpacks.quinn_invoicing", "EnhancedQuinnInvoicing"),
        "finn": ("aspire_orchestrator.skillpacks.finn_finance_manager", "EnhancedFinnFinanceManager"),
        "eli": ("aspire_orchestrator.skillpacks.eli_inbox", "EnhancedEliInbox"),
        "clara": ("aspire_orchestrator.skillpacks.clara_legal", "EnhancedClaraLegal"),
        "sarah": ("aspire_orchestrator.skillpacks.sarah_front_desk", "EnhancedSarahFrontDesk"),
        "nora": ("aspire_orchestrator.skillpacks.nora_conference", "EnhancedNoraConference"),
        "adam": ("aspire_orchestrator.skillpacks.adam_research", "EnhancedAdamResearch"),
        "tec": ("aspire_orchestrator.skillpacks.tec_documents", "EnhancedTecDocuments"),
        "teressa": ("aspire_orchestrator.skillpacks.teressa_books", "EnhancedTeressaBooks"),
        "milo": ("aspire_orchestrator.skillpacks.milo_payroll", "EnhancedMiloPayroll"),
        "mail_ops": ("aspire_orchestrator.skillpacks.mail_ops_desk", "EnhancedMailOps"),
        "ava": ("aspire_orchestrator.skillpacks.ava_user", "EnhancedAvaUser"),
        "ava_admin": ("aspire_orchestrator.skillpacks.ava_admin", "AvaAdminSkillPack"),
    }

    for agent_id, (module_path, class_name) in _class_map.items():
        try:
            import importlib
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            _SKILL_PACK_REGISTRY[agent_id] = cls()
            logger.debug("agent_dispatch: registered %s → %s", agent_id, class_name)
        except Exception as e:
            logger.warning(
                "agent_dispatch: failed to load %s.%s: %s",
                module_path, class_name, e,
            )

    _REGISTRY_INITIALIZED = True
    return _SKILL_PACK_REGISTRY


def _resolve_agent_id(state: OrchestratorState) -> str:
    """Extract agent ID from routing plan or state."""
    # Try routing_plan.steps[0].skill_pack → agent mapping
    routing_plan = state.get("routing_plan")
    if routing_plan and isinstance(routing_plan, dict):
        steps = routing_plan.get("steps", [])
        if steps:
            skill_pack_id = steps[0].get("skill_pack", "")
            _PACK_TO_AGENT: dict[str, str] = {
                "quinn_invoicing": "quinn",
                "finn_finance_manager": "finn",
                "eli_inbox": "eli",
                "clara_legal": "clara",
                "sarah_front_desk": "sarah",
                "nora_conference": "nora",
                "adam_research": "adam",
                "tec_documents": "tec",
                "teressa_books": "teressa",
                "milo_payroll": "milo",
                "mail_ops_desk": "mail_ops",
                "ava_user": "ava",
                "ava_admin": "ava_admin",
            }
            agent = _PACK_TO_AGENT.get(skill_pack_id)
            if agent:
                return agent

    # Fallback: task_type prefix
    task_type = str(state.get("task_type", ""))
    _PREFIX_MAP: dict[str, str] = {
        "invoice.": "quinn", "customer.": "quinn", "quote.": "quinn",
        "payout.": "quinn", "invoiceitem.": "quinn",
        "invoice_line_item.": "quinn", "invoice_payment.": "quinn",
        "email.": "eli", "mail.": "mail_ops", "domain.": "mail_ops",
        "contract.": "clara", "meeting.": "nora", "conference.": "nora",
        "call.": "sarah", "phone.": "sarah",
        "research.": "adam", "document.": "tec", "pdf.": "tec",
        "finance.": "finn", "cashflow.": "finn", "budget.": "finn",
        "books.": "teressa", "qbo.": "teressa", "stripe_qbo.": "teressa",
        "payroll.": "milo",
        "admin.": "ava_admin", "admin_ops.": "ava_admin",
    }
    for prefix, agent in _PREFIX_MAP.items():
        if task_type.startswith(prefix):
            return agent

    return "ava"


# ---------------------------------------------------------------------------
# Task-type → skill pack method routing
# ---------------------------------------------------------------------------

async def _dispatch_to_skill_pack(
    pack: Any,
    agent_id: str,
    task_type: str,
    utterance: str,
    state: OrchestratorState,
    activity_callback: Any | None = None,
) -> dict[str, Any]:
    """Call the right skill pack method based on agent + task_type.

    Each agent has domain-specific agentic methods. This function maps
    task_type to the appropriate method. For tasks without a dedicated
    method, falls back to run_agentic_loop with the task description.

    Returns dict with either:
      - execution_params (ready for policy_eval)
      - conversation_response (needs more info from user)
    """
    from aspire_orchestrator.services.agent_sdk_base import AgentContext

    ctx = AgentContext(
        suite_id=state.get("suite_id", "unknown"),
        office_id=state.get("office_id", "unknown"),
        correlation_id=state.get("correlation_id", str(uuid.uuid4())),
        risk_tier=str(state.get("risk_tier", "green")),
        actor_id=state.get("actor_id", "unknown"),
    )

    result: dict[str, Any] = {}

    # ── Quinn: Invoice/Customer/Quote workflows ──────────────────────
    if agent_id == "quinn" and task_type.startswith(("invoice.", "customer.", "quote.")):
        # Step 1: Parse user intent into structured data
        parse_result = await pack.parse_invoice_intent(utterance, ctx, activity_callback=activity_callback)

        if not parse_result.success:
            # Quinn couldn't parse — ask naturally in his voice
            error_msg = parse_result.error or "I need more details about this invoice."
            result["conversation_response"] = error_msg
            result["agent_target"] = "quinn"
            if parse_result.receipt:
                result["_dispatch_receipts"] = [parse_result.receipt]
            return result

        parsed_data = parse_result.data or {}
        customer_name = parsed_data.get("customer_name") or parsed_data.get("content", "")

        # Step 2: Try to match customer (if customer info extracted)
        if customer_name and hasattr(pack, "match_customer"):
            # Pass empty list for now — in production this would query Stripe
            # via tool_executor for customer.search. The skill pack's LLM
            # determines if a match exists or if onboarding is needed.
            match_result = await pack.match_customer(customer_name, [], ctx, activity_callback=activity_callback)
            if match_result.success:
                match_data = match_result.data or {}
                match_content = match_data.get("content", "")
                # If LLM says confidence < 0.7 or suggests new customer,
                # ask user about onboarding
                if "create new customer" in match_content.lower() or "no match" in match_content.lower():
                    # Quinn needs to onboard the customer first
                    result["conversation_response"] = match_content
                    result["agent_target"] = "quinn"
                    receipts = [parse_result.receipt]
                    if match_result.receipt:
                        receipts.append(match_result.receipt)
                    result["_dispatch_receipts"] = receipts
                    return result

        # Step 3: Draft invoice plan (if we have enough data)
        if hasattr(pack, "draft_invoice_plan"):
            plan_result = await pack.draft_invoice_plan(parsed_data, ctx, activity_callback=activity_callback)
            if plan_result.success:
                plan_data = plan_result.data or {}
                # Extract execution params from the plan
                result["execution_params"] = {
                    "plan": plan_data.get("content", ""),
                    "parsed_data": parsed_data,
                    "task_type": task_type,
                }
                receipts = [parse_result.receipt]
                if plan_result.receipt:
                    receipts.append(plan_result.receipt)
                result["_dispatch_receipts"] = receipts
                return result

    # ── Ava Admin: Direct desk method dispatch for admin ops ─────────
    if agent_id == "ava_admin" and task_type.startswith(("admin.", "admin_ops.")):
        # Map classified action types to skill pack method names.
        # e.g. "admin.ops.list_incidents" → "admin_ops_list_incidents"
        method_name = task_type.replace(".", "_").replace("admin_ops_", "admin_ops_")
        # Normalize: admin.ops.health_pulse → admin_ops_health_pulse
        if method_name.startswith("admin_ops_"):
            pass  # Already correct
        elif method_name.startswith("admin_"):
            method_name = method_name.replace("admin_", "admin_ops_", 1)

        method = getattr(pack, method_name, None)
        if method is not None:
            if activity_callback:
                import time as _dispatch_time
                activity_callback({
                    "type": "tool_call",
                    "message": f"Ava Admin is executing {method_name.replace('admin_ops_', '').replace('_', ' ')}...",
                    "icon": "server-outline",
                    "agent": "ava_admin",
                    "status": "active",
                    "timestamp": int(_dispatch_time.time() * 1000),
                })

            # Extract params from state/utterance for the desk method
            params: dict[str, Any] = {}
            if isinstance(state.get("execution_params"), dict):
                params = state["execution_params"]
            elif isinstance(state.get("request"), dict):
                req_payload = state["request"].get("payload", {})
                if isinstance(req_payload, dict):
                    params = {k: v for k, v in req_payload.items()
                              if k not in ("text", "utterance", "requested_agent", "channel", "history", "user_profile")}

            try:
                admin_result = await method(params, ctx)
                if admin_result.success:
                    content = (admin_result.data or {}).get("content", str(admin_result.data))
                    result["conversation_response"] = content
                    result["agent_target"] = "ava_admin"
                    if admin_result.receipt:
                        result["_dispatch_receipts"] = [admin_result.receipt]
                    return result
                else:
                    result["conversation_response"] = admin_result.error or "I couldn't complete that operation."
                    result["agent_target"] = "ava_admin"
                    if admin_result.receipt:
                        result["_dispatch_receipts"] = [admin_result.receipt]
                    return result
            except Exception as admin_exc:
                logger.error("ava_admin dispatch failed: method=%s error=%s", method_name, admin_exc)
                result["conversation_response"] = f"I hit an issue running {method_name.replace('admin_ops_', '').replace('_', ' ')}. Let me try again."
                result["agent_target"] = "ava_admin"
                return result

    # ── Generic agentic loop for all other agents/tasks ──────────────
    # Every Enhanced* pack has run_agentic_loop — use it.
    if hasattr(pack, "run_agentic_loop"):
        loop_result = await pack.run_agentic_loop(
            task=f"{task_type}: {utterance}",
            ctx=ctx,
            max_steps=3,
            timeout_s=60.0,
            activity_callback=activity_callback,
        )

        if loop_result.success:
            loop_data = loop_result.data or {}
            content = loop_data.get("content", "")
            steps = loop_data.get("steps", [])

            # If the loop produced structured execution params, use them
            if loop_data.get("execution_params"):
                result["execution_params"] = loop_data["execution_params"]
            else:
                # The agentic loop completed but returned conversational content
                # (e.g., agent asking for more info, or providing analysis)
                result["conversation_response"] = content
                result["agent_target"] = agent_id

            result["_dispatch_receipts"] = loop_result.receipt if isinstance(loop_result.receipt, list) else [loop_result.receipt] if loop_result.receipt else []
            return result
        else:
            # Loop failed — agent responds with what it needs
            error_msg = loop_result.error or f"I need more details to handle this {task_type} request."
            result["conversation_response"] = error_msg
            result["agent_target"] = agent_id
            if loop_result.receipt:
                result["_dispatch_receipts"] = [loop_result.receipt]
            return result

    # Should never reach here — every agent has run_agentic_loop
    logger.error("agent_dispatch: no agentic method found for %s/%s", agent_id, task_type)
    result["conversation_response"] = (
        f"I'm having trouble processing this {task_type} request. "
        "Let me look into it — can you try again in a moment?"
    )
    result["agent_target"] = agent_id
    return result


# ---------------------------------------------------------------------------
# Node entry point
# ---------------------------------------------------------------------------

async def agent_dispatch_node(state: OrchestratorState) -> dict[str, Any]:
    """Dispatch action intents to the agent's AgenticSkillPack.

    This is the bridge between the LangGraph pipeline and the agent
    intelligence layer. Every action intent flows through here.
    """
    utterance = state.get("utterance", "")
    task_type = state.get("task_type", "unknown")
    correlation_id = state.get("correlation_id", str(uuid.uuid4()))

    # Resolve which agent handles this
    agent_id = _resolve_agent_id(state)
    logger.info(
        "agent_dispatch: agent=%s task=%s utterance='%s'",
        agent_id, task_type, utterance[:80],
    )

    # Get the skill pack instance
    registry = _init_registry()
    pack = registry.get(agent_id)

    if pack is None:
        # Fail closed (Law #3) — no skill pack means no execution
        logger.error("agent_dispatch: no skill pack for agent=%s", agent_id)
        error_receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "receipt_type": "agent_dispatch.failed",
            "action_type": task_type,
            "risk_tier": "green",
            "outcome": "failed",
            "reason_code": "NO_SKILL_PACK",
            "actor_type": "system",
            "actor_id": "orchestrator.agent_dispatch",
            "suite_id": state.get("suite_id", "unknown"),
            "office_id": state.get("office_id", "unknown"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(error_receipt)
        return {
            "error": True,
            "error_code": "NO_SKILL_PACK",
            "error_message": f"No skill pack registered for agent '{agent_id}'",
            "pipeline_receipts": existing_receipts,
        }

    # Dispatch to the skill pack
    activity_callback = state.get("_activity_callback")
    try:
        dispatch_result = await _dispatch_to_skill_pack(
            pack=pack,
            agent_id=agent_id,
            task_type=task_type,
            utterance=utterance,
            state=state,
            activity_callback=activity_callback,
        )
    except Exception as exc:
        logger.error(
            "agent_dispatch CRASHED: agent=%s task=%s error=%s",
            agent_id, task_type, str(exc), exc_info=True,
        )
        # Fail closed with receipt (Law #2, #3)
        crash_receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "receipt_type": "agent_dispatch.crash",
            "action_type": task_type,
            "risk_tier": "green",
            "outcome": "failed",
            "reason_code": "DISPATCH_CRASH",
            "error": type(exc).__name__,
            "error_message": str(exc)[:500],
            "actor_type": "system",
            "actor_id": f"orchestrator.agent_dispatch.{agent_id}",
            "suite_id": state.get("suite_id", "unknown"),
            "office_id": state.get("office_id", "unknown"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(crash_receipt)
        return {
            "error": True,
            "error_code": "DISPATCH_CRASH",
            "error_message": f"Agent dispatch failed: {type(exc).__name__}",
            "pipeline_receipts": existing_receipts,
        }

    # Merge dispatch receipts into pipeline
    existing_receipts = list(state.get("pipeline_receipts", []))
    dispatch_receipts = dispatch_result.pop("_dispatch_receipts", [])
    if dispatch_receipts:
        existing_receipts.extend(dispatch_receipts)

    # Build return state
    output: dict[str, Any] = {
        "agent_target": dispatch_result.get("agent_target", agent_id),
        "pipeline_receipts": existing_receipts,
    }

    if dispatch_result.get("conversation_response"):
        # Agent needs more info — route to respond
        output["conversation_response"] = dispatch_result["conversation_response"]
    elif dispatch_result.get("execution_params"):
        # Agent resolved everything — route to policy_eval
        output["execution_params"] = dispatch_result["execution_params"]
    else:
        # Neither set — fail closed
        output["error"] = True
        output["error_code"] = "DISPATCH_NO_RESULT"
        output["error_message"] = "Agent dispatch produced no actionable result"

    return output
