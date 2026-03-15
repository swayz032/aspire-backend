"""Deterministic Narration Layer — Ava v1.5.

Ported from narration.ts (114 lines). Produces deterministic, template-based
response text for action outcomes. NEVER uses LLM for action narration.
LLM is only used for conversational responses (greetings, Q&A).

Key principle: "drafted"/"queued" verbs ONLY — NEVER "sent"/"created"/"paid".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _pick_owner_name(params: dict[str, Any] | None) -> str | None:
    if not params:
        return None
    p = params
    name = (
        (p.get("owner_profile") or {}).get("display_name")
        or p.get("owner_name")
        or p.get("user_name")
    )
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _pick_subject_name(params: dict[str, Any] | None) -> str | None:
    if not params:
        return None
    p = params
    # Match v1.5 narration.ts pickSubjectName() exactly — NO email fallback.
    # Fail-closed: if no display_name is available, return None → narration asks for client.
    name = (
        (p.get("subject_entity") or {}).get("display_name")
        or (p.get("client") or {}).get("display_name")
        or p.get("client_name")
        or p.get("customer_name")
        or p.get("contact_name")
    )
    if isinstance(name, str) and name.strip():
        return name.strip()

    # For contracts: extract counterparty from parties array
    # The "subject" is the non-owner party (role != owner_signer)
    parties = p.get("parties")
    if isinstance(parties, list) and parties:
        # Prefer the client_signer or first non-owner party
        for party in parties:
            if isinstance(party, dict) and party.get("role") != "owner_signer":
                pname = party.get("name", "").strip()
                if pname:
                    return pname
        # Fallback: just use first party name
        first = parties[0]
        if isinstance(first, dict):
            pname = first.get("name", "").strip()
            if pname:
                return pname

    return None


_TOKEN_HUMAN_LABELS: dict[str, str] = {
    "Sender.Company": "your company name",
    "Sender.FirstName": "your first name",
    "Sender.LastName": "your last name",
    "Sender.Email": "your email",
    "Sender.State": "your state",
    "Sender.Address": "your business address",
    "Sender.Phone": "your phone number",
    "Client.Company": "the other party's company name",
    "Client.FirstName": "the contact person's first name",
    "Client.LastName": "the contact person's last name",
    "Client.Email": "the contact person's email",
    "Client.State": "the other party's state",
    "Client.Address": "the other party's address",
    "Client.Phone": "the other party's phone number",
}


def _humanize_token_name(token_name: str) -> str:
    """Convert PandaDoc token name to human-readable label for narration."""
    return _TOKEN_HUMAN_LABELS.get(token_name, token_name.replace(".", " ").lower())


_CONTRACT_LABELS: dict[str, str] = {
    "general_mutual_nda": "a mutual NDA",
    "general_one_way_nda": "a one-way NDA",
    "trades_msa_lite": "a service agreement",
    "trades_sow": "a statement of work",
    "trades_estimate_quote_acceptance": "a quote acceptance",
    "trades_work_order": "a work order",
    "trades_change_order": "a change order",
    "trades_completion_acceptance": "a completion sign-off",
    "trades_subcontractor_agreement": "a subcontractor agreement",
    "trades_independent_contractor_agreement": "a contractor agreement",
    "acct_engagement_letter": "an engagement letter",
    "acct_scope_addendum": "a scope addendum",
    "acct_access_authorization": "an access authorization",
    "acct_fee_schedule_billing_auth": "a fee schedule",
    "acct_confidentiality_data_handling_addendum": "a confidentiality addendum",
    "landlord_residential_lease_base": "a residential lease",
    "landlord_lease_addenda_pack": "lease addenda",
    "landlord_renewal_extension_addendum": "a lease renewal",
    "landlord_move_in_checklist": "a move-in checklist",
    "landlord_move_out_checklist": "a move-out checklist",
    "landlord_security_deposit_itemization": "a deposit itemization",
    "landlord_notice_to_enter": "a notice to enter",
    # Trades proposals (real PandaDoc templates)
    "trades_painting_proposal": "a painting proposal",
    "trades_hvac_proposal": "an HVAC proposal",
    "trades_roofing_proposal": "a roofing proposal",
    "trades_architecture_proposal": "an architecture proposal",
    "trades_construction_proposal": "a construction proposal",
    "trades_residential_construction": "a residential construction proposal",
    "trades_residential_contract": "a residential construction contract",
    # Accounting / Tax
    "acct_tax_filing": "a tax filing form",
    # Landlord
    "landlord_commercial_sublease": "a commercial sublease",
    # General
    "general_w9": "a W-9 form",
    # Legacy aliases
    "nda": "a mutual NDA",
    "msa": "a service agreement",
    "sow": "a statement of work",
    "hvac": "an HVAC proposal",
    "roofing": "a roofing proposal",
    "painting": "a painting proposal",
    "construction": "a construction proposal",
    "architecture": "an architecture proposal",
    "sublease": "a commercial sublease",
    "accounting": "an accounting proposal",
    "w9": "a W-9 form",
}


def _template_label(template_type: str) -> str:
    """Return a human-friendly label for a template type."""
    return _CONTRACT_LABELS.get(template_type, "a contract draft")


def _needs_subject(task_type: str) -> bool:
    tt = task_type.lower()
    return any(kw in tt for kw in ("invoice", "contract", "proposal", "email", "sms", "whatsapp"))


def _action_verb(outcome: str, execution_params: dict[str, Any] | None = None) -> str:
    """Returns 'drafted' or 'queued'. NEVER 'sent'/'created'/'paid'.

    Matches v1.5 narration.ts actionVerb(): checks authority_queue/authority_item_id
    in payload before falling back to outcome-based logic.
    """
    p = execution_params or {}
    if p.get("authority_queue") is True or isinstance(p.get("authority_item_id"), str):
        return "queued"
    if outcome in ("pending", "queued"):
        return "queued"
    return "drafted"


def _money_from_params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    currency = params.get("currency", "USD")
    formatted = params.get("amount_display")
    if isinstance(formatted, str) and formatted.strip():
        return f" for {currency} {formatted.strip()}"

    cents = params.get("amount_cents")
    if isinstance(cents, (int, float)) and cents > 0:
        return f" for {currency} {cents / 100:.2f}"

    amt = params.get("amount")
    if isinstance(amt, (int, float)) and amt > 0:
        return f" for {currency} {amt:.2f}"
    if isinstance(amt, str) and amt.strip():
        return f" for {currency} {amt.strip()}"

    return ""


def compose_narration(
    outcome: str,
    task_type: str,
    tool_used: str | None,
    execution_params: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
    draft_id: str | None,
    risk_tier: str,
    owner_name: str | None = None,
    subject_name: str | None = None,
    channel: str | None = None,
) -> str:
    """Compose deterministic narration text for action outcomes.

    NEVER uses LLM. Template-based only.
    """
    tt = task_type.lower()
    owner = owner_name or _pick_owner_name(execution_params)
    subject = subject_name or _pick_subject_name(execution_params)

    header = f"{owner}: " if owner else ""

    # NEEDS_INFO outcome — Clara found the template but needs more data from user
    # This fires BEFORE document creation (preflight gate blocked it).
    # Must come BEFORE the subject check because needs_info doesn't require a subject.
    # Check both: outcome string AND execution_result error/data (pipeline sets outcome=failed)
    _er_check = execution_result or {}
    _is_needs_info = (
        outcome == "needs_info"
        or _er_check.get("error") == "needs_info"
        or _er_check.get("needs_info") is True
        or (isinstance(_er_check.get("data"), dict) and _er_check["data"].get("needs_info") is True)
    )
    if _is_needs_info:
        er = execution_result or {}
        # Data may be at top level (direct call) or nested under "data" key (via execute node)
        er_data = er.get("data", er) if isinstance(er.get("data"), dict) else er
        missing = er_data.get("missing_tokens", [])
        questions = er_data.get("suggested_questions", [])
        tpl_name = er_data.get("template_name", "the document")

        # Detect if we're collecting address/contact details (harder to get right via voice)
        _ADDRESS_TOKENS = {"Address", "City", "State", "Zip", "PostalCode", "StreetAddress", "Email", "Phone"}
        has_address_fields = any(
            t.split(".")[-1] in _ADDRESS_TOKENS for t in missing
        ) if missing else False
        is_voice = channel in ("voice", "video")

        # Voice verification prompt — addresses are easy to mishear
        chat_verify = ""
        if is_voice and has_address_fields:
            chat_verify = (
                " After you tell me, please glance at the chat to make sure "
                "I captured the addresses and details correctly."
            )

        if questions:
            # Use Ava's natural voice to ask the user
            q_text = " ".join(questions[:3])  # Max 3 questions at once
            return (
                f"{header}I found the right template for {tpl_name}, but I need a few "
                f"details before I can create it. {q_text}{chat_verify}"
            )
        elif missing:
            humanized = [_humanize_token_name(t) for t in missing[:4]]
            return (
                f"{header}I'm ready to draft {tpl_name}, but I need: "
                f"{', '.join(humanized)}. "
                f"Should I use your profile information, or would you like to provide different details?"
                f"{chat_verify}"
            )
        return (
            f"{header}I found the template but need a few more details. "
            "What information should I use for the parties on this document?"
        )

    # Fail-closed on personalization for subject-bound actions
    if not subject and _needs_subject(task_type):
        return (
            f"{header}I can prepare this, but I need the target client or contact first. "
            "Who should this be for?"
        )

    # PENDING outcome (draft created)
    if outcome in ("pending", "approval_required"):
        verb = _action_verb(outcome, execution_params)
        amount_str = _money_from_params(execution_params)

        if "invoice" in tt:
            return (
                f"{header}I {verb} an invoice for {subject}{amount_str}. "
                "It's in your Authority Queue — review the details to make sure "
                "everything is accurate, then approve or deny."
            )
        if "contract" in tt:
            tpl_type = (execution_params or {}).get("template_type", "")
            tpl_label = _template_label(tpl_type)
            is_voice = channel in ("voice", "video")

            # Specialist narration: when quality data available, Clara sounds expert
            er = execution_result or {}
            quality = er.get("document_quality", {})
            confidence = quality.get("confidence_score", 0)

            if confidence >= 90:
                filled = quality.get("tokens_filled", 0)
                total = quality.get("tokens_total", 0)
                specialist_parts = [
                    f"{header}I {verb} {tpl_label} for {subject}.",
                ]
                if total > 0:
                    specialist_parts.append(
                        f"{filled}/{total} fields filled -- {confidence}% confidence."
                    )
                # Content intelligence narration
                if quality.get("pricing_table_populated"):
                    pricing_notes = [n for n in quality.get("specialist_notes", []) if "pricing table" in n.lower()]
                    if pricing_notes:
                        specialist_parts.append(pricing_notes[0].capitalize() + ".")
                if quality.get("content_placeholders_populated"):
                    content_notes = [n for n in quality.get("specialist_notes", []) if "content section" in n.lower()]
                    if content_notes:
                        specialist_parts.append(content_notes[0].capitalize() + ".")
                warnings = quality.get("proactive_warnings", [])
                if warnings:
                    specialist_parts.append(warnings[0])
                specialist_parts.append(
                    "It's in your Authority Queue -- review and approve when ready."
                )
                return " ".join(specialist_parts)

            # Standard narration (no quality data or low confidence)
            verify_hint = (
                " I've listed all the details in the chat -- please review them there "
                "to make sure everything is spelled correctly before you approve."
                if is_voice else ""
            )
            return (
                f"{header}I {verb} {tpl_label} for {subject}. "
                f"It's in your Authority Queue -- open it to review the actual document "
                f"and verify the terms are accurate, then approve or deny.{verify_hint}"
            )
        if "email" in tt:
            return (
                f"{header}I {verb} an email draft to {subject}. "
                "It's in your Authority Queue — review the content to make sure "
                "it's accurate, then approve to send or deny to discard."
            )
        if "sms" in tt or "whatsapp" in tt:
            msg_channel = "WhatsApp message" if "whatsapp" in tt else "SMS message"
            return (
                f"{header}I {verb} a {msg_channel} draft to {subject}. "
                "It's in your Authority Queue — review the message to make sure "
                "it reads right, then approve to send or deny."
            )
        if "calendar" in tt:
            title = (execution_params or {}).get("title", "event")
            return (
                f"{header}I {verb} a calendar event: {title}. "
                "It's in your Authority Queue — check the time and details "
                "are correct, then approve or deny."
            )
        if "quote" in tt:
            return (
                f"{header}I {verb} a quote for {subject}{amount_str}. "
                "It's in your Authority Queue — review the line items and pricing "
                "for accuracy, then approve or deny."
            )

        if "payroll" in tt:
            return (
                f"{header}I {verb} a payroll run{amount_str}. "
                "It's in your Authority Queue — review the amounts and employee "
                "details to make sure everything is accurate, then approve or deny."
            )
        if "payment" in tt or "transfer" in tt:
            return (
                f"{header}I {verb} a payment{amount_str}. "
                "It's in your Authority Queue — review the recipient and amount "
                "for accuracy, then approve or deny."
            )

        # Generic pending
        risk_bit = (
            " (red-tier: requires video presence)" if risk_tier == "red"
            else " (medium-tier)" if risk_tier in ("medium", "yellow")
            else ""
        )
        return (
            f"{header}I {verb} a proposal{risk_bit}. "
            "It's in your Authority Queue — review it for accuracy, then approve or deny."
        )

    # SUCCESS outcome
    if outcome == "success":
        if "invoice" in tt:
            # invoice.create (GREEN) → draft created, pending send approval
            # invoice.send (resume) → actually sent
            er = execution_result or {}
            if er.get("authority_queue_id") or (er.get("data", {}).get("status") == "draft"):
                return (
                    f"{header}I've created a draft invoice for {subject}{_money_from_params(execution_params)}. "
                    "It's in your Authority Queue — review the details for accuracy, "
                    "then approve when ready to send."
                )
            return f"{header}Done — invoice for {subject}{_money_from_params(execution_params)} has been sent."
        if "email" in tt:
            return f"{header}Done — email sent to {subject}."
        if "calendar" in tt:
            title = (execution_params or {}).get("title", "event")
            return f"{header}Done — {title} added to your calendar."
        if "search" in tt or "research" in tt:
            return f"{header}Done — I found some results for you."
        if "contract" in tt:
            return f"{header}Done — contract sent to {subject} for signature."
        if "meeting" in tt or "conference" in tt:
            return f"{header}Done — your meeting room is ready."

        return f"{header}Done — that request is complete."

    # FAILED outcome — never expose raw error details to user (enterprise security)
    if outcome == "failed":
        er = execution_result or {}
        err_text = str(er.get("error") or er.get("reason_code") or "").upper()
        if "MODEL_UNAVAILABLE" in err_text:
            return (
                f"{header}I couldn't complete this because the model is temporarily unavailable. "
                "Please try again in a moment."
            )
        if "CHECKPOINTER_UNAVAILABLE" in err_text:
            return (
                f"{header}I couldn't access conversation memory for this task. "
                "Please try again in a moment."
            )
        if "TIMEOUT" in err_text:
            return (
                f"{header}This task timed out before completion. "
                "Would you like me to try again with a narrower request?"
            )
        if "AUTH" in err_text or "INVALID_KEY" in err_text:
            return (
                f"{header}I couldn't complete this because a required provider connection is missing or expired. "
                "Please check your provider connections and try again."
            )
        if "PROVIDER_ALL_FAILED" in err_text or "ALL PROVIDERS FAILED" in err_text:
            return (
                f"{header}I couldn't complete this because all research providers failed the request. "
                "Try a narrower query or retry in a moment."
            )
        return (
            f"{header}I wasn't able to complete this. "
            "Would you like me to try a different approach?"
        )

    # DENIED outcome
    if outcome == "denied":
        return f"{header}I can't perform that action — it was blocked by your security policy."

    # Fallback
    return f"{header}I handled that request."
