"""Param Extract Node — LLM parameter extraction from natural language.

Inserted between route and policy_eval in the 12-node pipeline.
Uses LLM Router model to extract structured tool parameters from user utterance.
Generates receipt for extraction attempt (Law #2).
Also builds advisor_context via context_builder.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Tool parameter schemas: required + optional fields per tool_id
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "stripe.invoice.create": {
        "required": ["customer_email", "amount_cents"],
        "optional": ["customer_name", "description", "currency", "due_days"],
    },
    "stripe.invoice.send": {
        "required": ["invoice_id"],
        "optional": [],
    },
    "stripe.quote.create": {
        "required": ["customer_email", "line_items"],
        "optional": ["customer_name", "expiry_days"],
    },
    "calendar.event.create": {
        "required": ["title", "start_time"],
        "optional": ["description", "event_type", "duration_minutes", "location", "participants"],
    },
    "polaris.email.send": {
        "required": ["to", "subject", "body"],
        "optional": ["cc", "bcc"],
    },
    "polaris.email.draft": {
        "required": ["to", "subject", "body"],
        "optional": ["cc", "bcc"],
    },
    "brave.search": {
        "required": ["query"],
        "optional": [],
    },
    "tavily.search": {
        "required": ["query"],
        "optional": [],
    },
    "livekit.room.create": {
        "required": ["room_name"],
        "optional": [],
    },
    "search.web": {
        "required": ["query"],
        "optional": [],
    },
    "search.places": {
        "required": ["query"],
        "optional": [],
    },
    "pandadoc.templates.list": {
        "required": [],
        "optional": ["q", "count", "tag"],
    },
    "pandadoc.templates.details": {
        "required": ["template_id"],
        "optional": [],
    },
    "pandadoc.contract.generate": {
        "required": ["template_type", "parties"],
        "optional": ["terms", "jurisdiction_state", "purpose", "term_length",
                      "governing_law"],
    },
    "pandadoc.contract.read": {
        "required": ["document_id"],
        "optional": [],
    },
    "pandadoc.contract.send": {
        "required": ["document_id"],
        "optional": ["message", "subject", "silent"],
    },
    "pandadoc.contract.sign": {
        "required": ["document_id", "message"],
        "optional": ["subject", "silent"],
    },
}


async def param_extract_node(state: OrchestratorState) -> dict[str, Any]:
    """Extract structured tool parameters from natural language.

    Uses LLM Router model (NOT hardcoded gpt-5-mini) to extract required+optional
    fields for the routed tool. Fails closed if required fields cannot be extracted.
    Also builds advisor_context via context_builder for v1.5 prompt pack awareness.
    """
    import httpx

    from aspire_orchestrator.config.settings import settings

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    utterance = state.get("utterance", "")
    task_type = state.get("task_type", "unknown")
    tool_used = state.get("tool_used")
    routing_plan = state.get("routing_plan", {})

    existing_receipts = list(state.get("pipeline_receipts", []))

    # Build advisor context (v1.5 integration)
    advisor_context = None
    try:
        from aspire_orchestrator.services.context_builder import build_advisor_context
        request = state.get("request")
        payload = {}
        if isinstance(request, dict):
            payload = request.get("payload", {})
        elif hasattr(request, "payload"):
            payload = request.payload if isinstance(request.payload, dict) else {}
        advisor_context = build_advisor_context(task_type, payload, suite_id)
    except Exception as e:
        logger.warning("Failed to build advisor_context: %s", e)

    # Extract request payload as fallback for execution_params
    # When LLM extraction isn't available, the client-supplied payload is used directly
    request = state.get("request")
    request_payload: dict[str, Any] = {}
    if isinstance(request, dict):
        request_payload = request.get("payload", {})
    elif hasattr(request, "payload"):
        request_payload = request.payload if isinstance(request.payload, dict) else {}

    # If no utterance or tool, fall back to request payload
    if not utterance or not tool_used:
        return {
            "execution_params": request_payload or None,
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
        }

    # Look up tool schema
    schema = _TOOL_SCHEMAS.get(tool_used, {})
    required_fields = schema.get("required", [])
    optional_fields = schema.get("optional", [])
    all_fields = required_fields + optional_fields

    if not all_fields:
        # Unknown tool — fall back to request payload
        return {
            "execution_params": request_payload or None,
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
        }

    # Build LLM prompt for parameter extraction
    schema_json = json.dumps({
        "tool": tool_used,
        "required": required_fields,
        "optional": optional_fields,
    }, indent=2)

    routing_context = json.dumps(routing_plan, default=str)[:500]

    # Tool-specific extraction hints for complex field structures
    _TOOL_HINTS: dict[str, str] = {
        "pandadoc.contract.generate": (
            "\n\nIMPORTANT for 'parties' field: Return an array of party objects. "
            "Each party MUST have separate fields — NEVER combine person name and company name.\n"
            "Party structure: {\n"
            "  \"name\": \"Person Full Name\",\n"
            "  \"company\": \"Company Name\",\n"
            "  \"email\": \"email@example.com\",\n"
            "  \"role\": \"sender|client\",\n"
            "  \"address\": \"Street Address\",\n"
            "  \"city\": \"City\",\n"
            "  \"state\": \"State abbreviation (e.g. TX, CA, NY)\",\n"
            "  \"zip\": \"ZIP/Postal Code\",\n"
            "  \"phone\": \"Phone Number\"\n"
            "}\n"
            "- 'name' = the PERSON's name only (e.g. \"Bruce Wayne\"), never the company\n"
            "- 'company' = the COMPANY name only (e.g. \"Wayne Enterprises\"), separate from person\n"
            "- 'role' = \"sender\" for the party sending/creating the document, \"client\" for the counterparty/signer\n"
            "- If the user mentions 'from X' or 'I am X' or 'my company', that's the sender\n"
            "- If the user mentions 'to X' or 'send to X' or 'the client', that's the client\n"
            "- Extract ALL address details mentioned: street address, city, state, ZIP code\n"
            "- Extract email addresses and phone numbers for each party if mentioned\n"
            "- If 'governed by Texas law' or similar, set jurisdiction_state to the state abbreviation\n"
            "\nFor 'template_type': Use lowercase registry keys like 'nda', 'mutual_nda', "
            "'msa', 'sow', 'subcontractor', 'lease', etc. NOT full sentences.\n"
            "\nFor 'governing_law': If the user mentions governing law or jurisdiction "
            "(e.g. 'governed by Texas law'), extract the state name or abbreviation."
        ),
        "pandadoc.contract.send": (
            "\nFor 'document_id': Extract the PandaDoc document UUID if mentioned."
        ),
    }
    tool_hint = _TOOL_HINTS.get(tool_used, "")

    prompt = (
        f"Extract structured parameters from the user's request for the tool '{tool_used}'.\n\n"
        f"Tool schema:\n{schema_json}\n\n"
        f"User request: \"{utterance}\"\n"
        f"Routing context: {routing_context}\n"
        f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        "Return ONLY a JSON object with the extracted fields. "
        "Use null for any field you cannot extract from the request. "
        "For amount_cents, convert dollar amounts to cents (e.g., $49 = 4900). "
        "For dates/times, use ISO 8601 format.\n"
        f"Response format: {{\"field1\": \"value1\", \"field2\": \"value2\"}}"
        f"{tool_hint}"
    )

    # Call LLM via router model (NOT hardcoded gpt-5-mini)
    api_key = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = settings.router_model_classifier  # CHEAP_CLASSIFIER for extraction

    extracted_params = None
    extraction_error = None

    if api_key:
        try:
            _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))
            messages = [{"role": "developer" if _is_reasoning else "system", "content": "You are a precise parameter extractor. Return only valid JSON."}]
            messages.append({"role": "user", "content": prompt})

            payload_llm: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_completion_tokens": 1024,
            }
            if not _is_reasoning:
                payload_llm["temperature"] = 0.0

            with httpx.Client(timeout=25) as client:
                resp = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=payload_llm,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Parse JSON from LLM response
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content
                content = content.rsplit("```", 1)[0]
                content = content.strip()

            extracted_params = json.loads(content)
            logger.info("Param extraction success for %s: %d fields — %s", tool_used, len(extracted_params), json.dumps(extracted_params, default=str)[:500])

        except json.JSONDecodeError as e:
            extraction_error = f"Failed to parse LLM response as JSON: {e}"
            logger.warning("Param extraction JSON parse failed for %s: %s", tool_used, e)
        except Exception as e:
            extraction_error = f"LLM call failed: {e}"
            logger.warning("Param extraction LLM failed for %s: %s", tool_used, e)

    else:
        extraction_error = "No API key available for parameter extraction"
        logger.warning("No API key for param extraction — skipping LLM call")

    # Merge LLM-extracted params with request payload.
    # LLM extracts from natural language utterance; request payload has structured data.
    # Rule: structured payload WINS for complex fields (lists/dicts) because the LLM
    # simplifies them (e.g., parties: [{name, email, role}] → ["Acme Corp"]).
    # LLM wins for simple scalar fields it extracted from the utterance.
    if extracted_params and request_payload:
        for key, val in request_payload.items():
            if key == "utterance" or key == "text":
                continue  # Don't copy utterance into execution params
            existing = extracted_params.get(key)
            # Structured payload always wins for complex types (lists of dicts, nested objects)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                extracted_params[key] = val
            elif isinstance(val, dict) and val:
                # Merge dict fields: request payload fills gaps in LLM-extracted dict
                if isinstance(existing, dict):
                    merged = {**val, **{k: v for k, v in existing.items() if v is not None}}
                    extracted_params[key] = merged
                else:
                    extracted_params[key] = val
            elif existing is None or (isinstance(existing, str) and not existing.strip()):
                extracted_params[key] = val
        logger.info("Merged request payload into extracted params: %d total keys", len(extracted_params))

    # Validate required fields
    missing_fields = []
    if extracted_params and required_fields:
        for field in required_fields:
            val = extracted_params.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing_fields.append(field)

    # Receipt for extraction attempt (Law #2)
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "orchestrator.param_extract",
        "action_type": f"param_extract.{task_type}",
        "risk_tier": "green",
        "tool_used": tool_used or "unknown",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "success" if (extracted_params and not missing_fields) else "failed",
        "reason_code": "PARAMS_EXTRACTED" if (extracted_params and not missing_fields) else "PARAM_EXTRACTION_FAILED",
        "receipt_type": ReceiptType.PARAM_EXTRACTION.value,
        "receipt_hash": "",
    }
    existing_receipts.append(receipt)

    # Fail-closed on missing required fields
    if missing_fields:
        field_list = ", ".join(missing_fields)
        logger.info("Param extraction missing required fields for %s: %s", tool_used, field_list)
        return {
            "execution_params": None,
            "error_code": "PARAM_EXTRACTION_FAILED",
            "error_message": f"I need more details to proceed. Please provide: {field_list}",
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
        }

    if extraction_error and not extracted_params:
        # Graceful degradation: if no API key (test/dev) or LLM failed,
        # fall back to request payload so pipeline continues with real execution.
        # Only fail-closed on missing REQUIRED fields (above).
        logger.info(
            "Param extraction unavailable for %s — falling back to request payload: %s",
            tool_used, extraction_error,
        )
        return {
            "execution_params": request_payload or None,
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
        }

    return {
        "execution_params": extracted_params,
        "advisor_context": advisor_context,
        "pipeline_receipts": existing_receipts,
    }
