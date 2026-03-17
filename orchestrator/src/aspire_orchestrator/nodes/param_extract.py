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
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.services.eli_email_param_helpers import (
    apply_email_tweaks,
    body_text_to_html,
    extract_emails,
    extract_labeled_email,
    extract_subject_hint,
    infer_subject_from_utterance,
    is_email_tweak_request,
    naturalize_email_body,
    strip_html,
    synthesize_body_text,
)
from aspire_orchestrator.services.eli_agentic_rag import run_eli_agentic_rag
from aspire_orchestrator.services.openai_client import generate_json_async
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
        "required": ["from_address", "to", "subject", "body_text"],
        "optional": ["body_html", "cc", "bcc", "reply_to"],
    },
    "polaris.email.draft": {
        "required": ["from_address", "to", "subject", "body_text"],
        "optional": ["body_html", "cc", "bcc", "reply_to"],
    },
    "polaris.email.read": {
        "required": [],
        "optional": ["folder", "unread_only", "limit", "since"],
    },
    "internal.office.read": {
        "required": [],
        "optional": ["folder", "unread_only", "limit"],
    },
    "internal.office.create": {
        "required": ["recipient_suite_id", "recipient_office_id", "title", "body"],
        "optional": ["priority"],
    },
    "internal.office.draft": {
        "required": ["recipient_suite_id", "recipient_office_id", "title", "body"],
        "optional": ["priority"],
    },
    "internal.office.send": {
        "required": ["draft_id"],
        "optional": ["recipient_suite_id", "recipient_office_id", "title", "body", "priority"],
    },
    "internal.email.triage": {
        "required": ["email_id"],
        "optional": ["subject", "body"],
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
        "required": ["name"],
        "optional": ["empty_timeout", "max_participants"],
    },
    "livekit.meeting.schedule": {
        "required": ["participants", "time"],
        "optional": ["agenda", "room_name"],
    },
    "deepgram.transcribe": {
        "required": ["audio_url"],
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
    "search.image": {
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
    from aspire_orchestrator.config.settings import settings

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    utterance = state.get("utterance", "")
    task_type = state.get("task_type", "unknown")
    routing_plan = state.get("routing_plan", {})

    # H15: Validate routing_plan exists before proceeding
    if not routing_plan:
        logger.warning("param_extract: no routing_plan — cannot extract params")
        return {
            "error": True,
            "error_code": "MISSING_ROUTING_PLAN",
            "error_message": "No routing plan available for parameter extraction",
            "execution_params": None,
            "pipeline_receipts": list(state.get("pipeline_receipts", [])),
        }

    # H4: Set tool_used from routing_plan steps (not just state)
    tool_used = state.get("tool_used")
    if not tool_used:
        steps = routing_plan.get("steps") if isinstance(routing_plan, dict) else getattr(routing_plan, "steps", None)
        if steps and len(steps) > 0:
            step = steps[0]
            tool_used = step.get("tool_id") if isinstance(step, dict) else getattr(step, "tool_id", None)
            logger.info("param_extract: resolved tool_used=%s from routing_plan", tool_used)

    existing_receipts = list(state.get("pipeline_receipts", []))
    eli_agentic_meta: dict[str, Any] = {}

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

    # If no tool mapping, we cannot validate against a schema.
    if not tool_used:
        return {
            "execution_params": request_payload or None,
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
            "tool_used": tool_used,
        }

    # For direct API calls (task_type + payload, no utterance), still validate required
    # fields so YELLOW flows cannot create approvals with incomplete params.
    if not utterance:
        schema = _TOOL_SCHEMAS.get(tool_used, {})
        required_fields = schema.get("required", [])
        missing_fields: list[str] = []
        if required_fields:
            for field in required_fields:
                val = request_payload.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    missing_fields.append(field)

        if missing_fields:
            field_list = ", ".join(missing_fields)
            receipt = {
                "id": str(uuid.uuid4()),
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "office_id": office_id,
                "actor_type": "system",
                "actor_id": "orchestrator.param_extract",
                "action_type": f"param_extract.{task_type}",
                "risk_tier": "green",
                "tool_used": tool_used,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "outcome": "failed",
                "reason_code": "PARAM_EXTRACTION_FAILED",
                "receipt_type": ReceiptType.PARAM_EXTRACTION.value,
                "receipt_hash": "",
            }
            existing_receipts.append(receipt)
            return {
                "execution_params": None,
                "error_code": "PARAM_EXTRACTION_FAILED",
                "error_message": f"I need more details to proceed. Please provide: {field_list}",
                "advisor_context": advisor_context,
                "pipeline_receipts": existing_receipts,
            }

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
        "Use ISO 8601 ONLY for structured schedule fields (e.g., start_time, end_time, due_date, expires_at). "
        "Keep human-facing message fields (subject, body, body_text, body_html, title) in natural conversational language.\n"
        f"Response format: {{\"field1\": \"value1\", \"field2\": \"value2\"}}"
        f"{tool_hint}"
    )

    # Call LLM via router model (NOT hardcoded gpt-5-mini)
    api_key = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = settings.router_model_classifier  # CHEAP_CLASSIFIER for extraction

    extracted_params = None
    extraction_error = None
    email_tweak_request = False

    if api_key:
        try:
            _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))
            messages = [{"role": "developer" if _is_reasoning else "system", "content": "You are a precise parameter extractor. Return only valid JSON."}]
            messages.append({"role": "user", "content": prompt})

            extracted_params = await generate_json_async(
                model=model,
                messages=messages,
                api_key=api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=25.0,
                max_output_tokens=1024,
                temperature=None if _is_reasoning else 0.0,
                prefer_responses_api=True,
            )
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

    # Tool-specific post-processing before required-field validation.
    # 1) conference room creation: normalize `room_name` -> `name` and
    #    synthesize if missing to avoid needless hard-fail.
    if tool_used == "livekit.room.create" and isinstance(extracted_params, dict):
        if extracted_params.get("name") in (None, "") and extracted_params.get("room_name"):
            extracted_params["name"] = extracted_params.get("room_name")
        extracted_params.pop("room_name", None)
        room_name = extracted_params.get("name")
        if room_name is None or (isinstance(room_name, str) and not room_name.strip()):
            short_id = uuid.uuid4().hex[:8]
            extracted_params["name"] = f"conference-{short_id}"

    # 2) email.draft/send: robust recipient/sender/subject/body recovery for
    # natural language prompts ("Eli, draft email to X from Y subject Z ...").
    if tool_used in ("polaris.email.draft", "polaris.email.send") and isinstance(extracted_params, dict):
        prior_params = state.get("execution_params") if isinstance(state.get("execution_params"), dict) else {}
        tweak_request = is_email_tweak_request(utterance)
        email_tweak_request = tweak_request

        emails = extract_emails(utterance)
        labeled_to = extract_labeled_email(utterance, "recipient") or extract_labeled_email(utterance, "to")
        labeled_from = extract_labeled_email(utterance, "sender") or extract_labeled_email(utterance, "from")
        # Prefer explicit "from X" capture for sender.
        from_match = re.search(
            r"\bfrom\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
            utterance,
            re.IGNORECASE,
        )
        explicit_from = labeled_from or (from_match.group(1).strip() if from_match else None)

        to_val = extracted_params.get("to")
        if to_val is None or (isinstance(to_val, str) and not to_val.strip()):
            if labeled_to:
                extracted_params["to"] = labeled_to
            # Examples: "email to ACME ...", "draft a follow-up to John ..."
            m = re.search(r"\bto\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", utterance, re.IGNORECASE)
            if m and extracted_params.get("to") in (None, ""):
                candidate = m.group(1).strip()
                if candidate:
                    extracted_params["to"] = candidate
            m_name = re.search(r"\bto\s+([-A-Za-z0-9& ]{2,80}?)(?:,| about | regarding | on | with |$)", utterance, re.IGNORECASE)
            if m_name and extracted_params.get("to") in (None, ""):
                candidate_name = m_name.group(1).strip()
                bad_fragments = ("call to action", "action", "tone should", "include ", "subject")
                if candidate_name and not any(f in candidate_name.lower() for f in bad_fragments):
                    extracted_params["to"] = candidate_name
            if extracted_params.get("to") in (None, "") and emails:
                # If the first/only email appears in utterance, use it as recipient.
                # If we also have explicit sender, pick the non-sender email as recipient.
                if explicit_from and len(emails) >= 2:
                    recipients = [e for e in emails if e.lower() != explicit_from.lower()]
                    if recipients:
                        extracted_params["to"] = recipients[0]
                else:
                    extracted_params["to"] = emails[0]

        if (extracted_params.get("from_address") in (None, "")) and (labeled_from or explicit_from):
            extracted_params["from_address"] = explicit_from
        elif extracted_params.get("from_address") in (None, "") and len(emails) >= 2:
            # Heuristic fallback: if we see two emails, second is often sender.
            extracted_params["from_address"] = emails[1]

        if extracted_params.get("subject") in (None, ""):
            subject_hint = extract_subject_hint(utterance)
            if subject_hint:
                extracted_params["subject"] = subject_hint
            elif not tweak_request:
                extracted_params["subject"] = infer_subject_from_utterance(utterance)

        body_text = extracted_params.get("body_text")
        body_html = extracted_params.get("body_html")
        if (body_text in (None, "")) and (body_html in (None, "")):
            to_email = str(extracted_params.get("to", "")).strip()
            subject = str(extracted_params.get("subject", "Quick Follow-Up")).strip()
            if to_email:
                composed = synthesize_body_text(
                    to_email=to_email,
                    subject=subject,
                    utterance=utterance,
                    from_address=str(extracted_params.get("from_address", "")).strip() or None,
                )
                extracted_params["body_text"] = composed
                extracted_params["body_html"] = body_text_to_html(composed)
        elif body_text in (None, "") and isinstance(body_html, str) and body_html.strip():
            extracted_params["body_text"] = strip_html(body_html)
        elif body_html in (None, "") and isinstance(body_text, str) and body_text.strip():
            extracted_params["body_html"] = body_text_to_html(body_text)

        # Always normalize machine-like phrasing in user-facing email body.
        bt = extracted_params.get("body_text")
        if isinstance(bt, str) and bt.strip():
            normalized = naturalize_email_body(bt)
            extracted_params["body_text"] = normalized
            if extracted_params.get("body_html") in (None, ""):
                extracted_params["body_html"] = body_text_to_html(normalized)

        # Conversational tweak loop: if user says "make it warmer/shorter/etc"
        # and we have a prior draft in thread state, carry forward required fields.
        if tweak_request and isinstance(prior_params, dict):
            for field in ("from_address", "to", "subject", "body_text", "body_html"):
                if extracted_params.get(field) in (None, "") and prior_params.get(field) not in (None, ""):
                    extracted_params[field] = prior_params.get(field)

            existing_subject = str(extracted_params.get("subject", "")).strip()
            existing_body = str(extracted_params.get("body_text", "")).strip()
            if existing_subject and existing_body:
                tweaked_subject, tweaked_body = apply_email_tweaks(
                    subject=existing_subject,
                    body_text=existing_body,
                    utterance=utterance,
                )
                extracted_params["subject"] = tweaked_subject
                extracted_params["body_text"] = tweaked_body
                extracted_params["body_html"] = body_text_to_html(tweaked_body)

        # Agentic Eli RAG pass (always-on with production fallback).
        assigned_agent = ""
        try:
            steps = routing_plan.get("steps", []) if isinstance(routing_plan, dict) else []
            if steps and isinstance(steps[0], dict) and str(steps[0].get("skill_pack", "")).strip() == "eli_inbox":
                assigned_agent = "eli"
            else:
                assigned_agent = str(state.get("agent_target", "")).strip().lower()
        except Exception:
            assigned_agent = str(state.get("agent_target", "")).strip().lower()

        extracted_params, eli_agentic_meta = await run_eli_agentic_rag(
            task_type=task_type,
            assigned_agent=assigned_agent,
            utterance=utterance,
            suite_id=suite_id,
            params=extracted_params,
        )

    # 3) email.read: merge classifier entities (e.g., unread + limit) into params.
    if tool_used == "polaris.email.read":
        if not isinstance(extracted_params, dict):
            extracted_params = {}
        intent_result = state.get("intent_result") or {}
        entities = intent_result.get("entities", {}) if isinstance(intent_result, dict) else {}
        if isinstance(entities, dict):
            for key in ("folder", "unread_only", "limit", "since"):
                val = entities.get(key)
                if val is not None and key not in extracted_params:
                    extracted_params[key] = val
        if "folder" not in extracted_params:
            extracted_params["folder"] = "inbox"
        if "limit" not in extracted_params:
            extracted_params["limit"] = 5

    # 4) office.read: merge classifier entities + defaults.
    if tool_used == "internal.office.read":
        if not isinstance(extracted_params, dict):
            extracted_params = {}
        intent_result = state.get("intent_result") or {}
        entities = intent_result.get("entities", {}) if isinstance(intent_result, dict) else {}
        if isinstance(entities, dict):
            for key in ("folder", "unread_only", "limit"):
                val = entities.get(key)
                if val is not None and key not in extracted_params:
                    extracted_params[key] = val
        if "folder" not in extracted_params:
            extracted_params["folder"] = "inbox"
        if "limit" not in extracted_params:
            extracted_params["limit"] = 10

    # 5) office create/draft: recover recipient identifiers from utterance hints.
    if tool_used in ("internal.office.create", "internal.office.draft") and isinstance(extracted_params, dict):
        recipient_suite_id = extracted_params.get("recipient_suite_id")
        recipient_office_id = extracted_params.get("recipient_office_id")
        if recipient_suite_id in (None, ""):
            m = re.search(r"\bsuite\s*(id)?\s*[:#]?\s*([0-9a-fA-F-]{8,36})", utterance, re.IGNORECASE)
            if m:
                extracted_params["recipient_suite_id"] = m.group(2)
        if recipient_office_id in (None, ""):
            m = re.search(r"\boffice\s*(id)?\s*[:#]?\s*([0-9a-fA-F-]{8,36})", utterance, re.IGNORECASE)
            if m:
                extracted_params["recipient_office_id"] = m.group(2)
        if "priority" not in extracted_params:
            extracted_params["priority"] = "NORMAL"

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
        # Human-friendly field name mapping (raw schema keys → user-facing)
        _friendly_names: dict[str, str] = {
            "customer_email": "customer email",
            "customer_name": "customer name",
            "amount_cents": "amount",
            "amount": "amount",
            "currency": "currency",
            "due_date": "due date",
            "invoice_number": "invoice number",
            "to": "recipient email",
            "from_address": "sender email",
            "subject": "subject",
            "body": "message body",
            "recipient_office_id": "recipient",
            "description": "description",
            "payment_method": "payment method",
            "account_id": "account",
            "contact_name": "contact name",
            "contact_email": "contact email",
            "document_title": "document title",
            "start_time": "start time",
            "end_time": "end time",
        }
        friendly_fields = [_friendly_names.get(f, f.replace("_", " ")) for f in missing_fields]
        field_list = ", ".join(friendly_fields)
        error_message = f"I need more details to proceed. Please provide: {field_list}"
        if (
            tool_used in ("polaris.email.draft", "polaris.email.send")
            and email_tweak_request
            and any(f in missing_fields for f in ("to", "from_address"))
        ):
            error_message = (
                "To revise this email, I need the recipient and sender context. "
                "Please include `Recipient:` and `Sender:` (or paste the current draft headers), "
                "then I can rewrite it immediately."
            )
        logger.info("Param extraction missing required fields for %s: %s", tool_used, field_list)
        return {
            "execution_params": None,
            "error_code": "PARAM_EXTRACTION_FAILED",
            "error_message": error_message,
            "advisor_context": advisor_context,
            "pipeline_receipts": existing_receipts,
            "eli_rag_status": eli_agentic_meta.get("eli_rag_status"),
            "eli_fallback_mode": eli_agentic_meta.get("eli_fallback_mode"),
            "eli_rag_sources": eli_agentic_meta.get("eli_rag_sources"),
            "eli_iteration_count": eli_agentic_meta.get("eli_iteration_count"),
            "eli_agentic_plan": eli_agentic_meta.get("eli_agentic_plan"),
            "eli_quality_report": eli_agentic_meta.get("eli_quality_report"),
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
            "eli_rag_status": eli_agentic_meta.get("eli_rag_status"),
            "eli_fallback_mode": eli_agentic_meta.get("eli_fallback_mode"),
            "eli_rag_sources": eli_agentic_meta.get("eli_rag_sources"),
            "eli_iteration_count": eli_agentic_meta.get("eli_iteration_count"),
            "eli_agentic_plan": eli_agentic_meta.get("eli_agentic_plan"),
            "eli_quality_report": eli_agentic_meta.get("eli_quality_report"),
        }

    return {
        "execution_params": extracted_params,
        "advisor_context": advisor_context,
        "pipeline_receipts": existing_receipts,
        "tool_used": tool_used,
        "eli_rag_status": eli_agentic_meta.get("eli_rag_status"),
        "eli_fallback_mode": eli_agentic_meta.get("eli_fallback_mode"),
        "eli_rag_sources": eli_agentic_meta.get("eli_rag_sources"),
        "eli_iteration_count": eli_agentic_meta.get("eli_iteration_count"),
        "eli_agentic_plan": eli_agentic_meta.get("eli_agentic_plan"),
        "eli_quality_report": eli_agentic_meta.get("eli_quality_report"),
    }
