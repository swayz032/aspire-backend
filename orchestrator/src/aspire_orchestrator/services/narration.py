"""Deterministic Narration Layer — Ava v2.0 (Conversational).

Produces warm, voice-first narration for all action outcomes.
Template-based only — NEVER uses LLM for action narration.
LLM is only used for conversational responses (greetings, Q&A).

Tone: Ava speaks like a trusted executive assistant. Natural, warm,
confident. No robotic "Done — X." patterns. Every response sounds like
something a real person would say out loud.

Key principle: "drafted"/"queued" verbs ONLY — NEVER "sent"/"created"/"paid".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
    name = (
        (p.get("subject_entity") or {}).get("display_name")
        or (p.get("client") or {}).get("display_name")
        or p.get("client_name")
        or p.get("customer_name")
        or p.get("contact_name")
    )
    if isinstance(name, str) and name.strip():
        return name.strip()

    parties = p.get("parties")
    if isinstance(parties, list) and parties:
        for party in parties:
            if isinstance(party, dict) and party.get("role") != "owner_signer":
                pname = (party.get("name") or "").strip()
                if pname:
                    return pname
        first = parties[0]
        if isinstance(first, dict):
            pname = (first.get("name") or "").strip()
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
    "trades_painting_proposal": "a painting proposal",
    "trades_hvac_proposal": "an HVAC proposal",
    "trades_roofing_proposal": "a roofing proposal",
    "trades_architecture_proposal": "an architecture proposal",
    "trades_construction_proposal": "a construction proposal",
    "trades_residential_construction": "a residential construction proposal",
    "trades_residential_contract": "a residential construction contract",
    "acct_tax_filing": "a tax filing form",
    "landlord_commercial_sublease": "a commercial sublease",
    "general_w9": "a W-9 form",
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
    """Returns 'drafted' or 'queued'. NEVER 'sent'/'created'/'paid'."""
    p = execution_params or {}
    if p.get("authority_queue") is True or isinstance(p.get("authority_item_id"), str):
        return "queued"
    if outcome in ("pending", "queued"):
        return "queued"
    return "drafted"


_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$",
    "JPY": "¥", "CHF": "CHF ", "MXN": "MX$", "BRL": "R$",
}


def _format_money(amount: float, currency: str) -> str:
    """Format money with currency symbol (e.g., $4,200.00)."""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency} ")
    return f"{symbol}{amount:,.2f}"


def _money_from_params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    currency = params.get("currency") or "USD"
    formatted = params.get("amount_display")
    if isinstance(formatted, str) and formatted.strip():
        return f" for {formatted.strip()}"

    cents = params.get("amount_cents")
    if isinstance(cents, (int, float)) and cents > 0:
        return f" for {_format_money(cents / 100, currency)}"

    amt = params.get("amount")
    if isinstance(amt, (int, float)) and amt > 0:
        return f" for {_format_money(amt, currency)}"
    if isinstance(amt, str) and amt.strip():
        return f" for {currency} {amt.strip()}"

    return ""


def _format_timestamp(ts: int | None) -> str | None:
    """Convert unix timestamp to human-readable date string."""
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%B %d, %Y")
    except (ValueError, OSError):
        return None


def _payout_amount_str(amount: int, currency: str = "usd") -> str:
    """Format payout amount (cents → dollars)."""
    cur = (currency or "usd").upper()
    if cur == "USD":
        return f"${amount / 100:,.2f}"
    return f"{amount / 100:,.2f} {cur}"


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
    """Compose warm, voice-first narration for action outcomes.

    Every template sounds like something Ava would actually say out loud.
    NEVER uses LLM. Template-based only.
    """
    tt = task_type.lower()
    owner = owner_name or _pick_owner_name(execution_params)
    subject = subject_name or _pick_subject_name(execution_params)

    header = f"{owner}: " if owner else ""

    # ── NEEDS_INFO — Clara found the template but needs more data ────────
    _er_check = execution_result or {}
    _is_needs_info = (
        outcome == "needs_info"
        or _er_check.get("error") == "needs_info"
        or _er_check.get("needs_info") is True
        or (isinstance(_er_check.get("data"), dict) and _er_check["data"].get("needs_info") is True)
    )
    if _is_needs_info:
        er = execution_result or {}
        er_data = er.get("data", er) if isinstance(er.get("data"), dict) else er
        missing = er_data.get("missing_tokens", [])
        questions = er_data.get("suggested_questions", [])
        tpl_name = er_data.get("template_name", "the document")

        _ADDRESS_TOKENS = {"Address", "City", "State", "Zip", "PostalCode", "StreetAddress", "Email", "Phone"}
        has_address_fields = any(
            t.split(".")[-1] in _ADDRESS_TOKENS for t in missing
        ) if missing else False
        is_voice = channel in ("voice", "video")

        chat_verify = ""
        if is_voice and has_address_fields:
            chat_verify = (
                " After you tell me, take a quick look at the chat to make sure "
                "I got the addresses and details right."
            )

        if questions:
            q_text = " ".join(questions[:3])
            return (
                f"{header}I found the right template for {tpl_name}, but I need a few "
                f"details before I can put it together. {q_text}{chat_verify}"
            )
        elif missing:
            humanized = [_humanize_token_name(t) for t in missing[:4]]
            return (
                f"{header}I'm ready to draft {tpl_name}, but I still need: "
                f"{', '.join(humanized)}. "
                f"Want me to pull from your profile, or would you rather give me different details?"
                f"{chat_verify}"
            )
        return (
            f"{header}I found the template, but I need a few more details from you. "
            "Who are the parties on this document?"
        )

    # ── Subject gate — personalization required ──────────────────────────
    if not subject and _needs_subject(task_type):
        return (
            f"{header}I can get this ready, but I need to know who it's for. "
            "What's the client or contact name?"
        )

    # ── PENDING — draft created, waiting in authority queue ──────────────
    if outcome in ("pending", "approval_required"):
        verb = _action_verb(outcome, execution_params)
        amount_str = _money_from_params(execution_params)

        if "invoice" in tt:
            return (
                f"{header}I've {verb} an invoice for {subject}{amount_str}. "
                "It's sitting in your Authority Queue now — take a look at the details, "
                "make sure everything checks out, and approve it when you're ready."
            )
        if "contract" in tt:
            tpl_type = (execution_params or {}).get("template_type", "")
            tpl_label = _template_label(tpl_type)
            is_voice = channel in ("voice", "video")

            er = execution_result or {}
            quality = er.get("document_quality", {})
            confidence = quality.get("confidence_score", 0)

            if confidence >= 90:
                filled = quality.get("tokens_filled", 0)
                total = quality.get("tokens_total", 0)
                specialist_parts = [
                    f"{header}I've put together {tpl_label} for {subject}.",
                ]
                if total > 0:
                    specialist_parts.append(
                        f"I filled in {filled} out of {total} fields — {confidence}% confidence."
                    )
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
                    "It's in your Authority Queue — give it a read and approve when you're satisfied."
                )
                return " ".join(specialist_parts)

            verify_hint = (
                " I've listed all the details in the chat — double-check the spelling "
                "and terms there before you approve."
                if is_voice else ""
            )
            return (
                f"{header}I've {verb} {tpl_label} for {subject}. "
                f"It's in your Authority Queue — open it up, review the terms, "
                f"and approve or deny when you're ready.{verify_hint}"
            )
        if "email" in tt:
            return (
                f"{header}I've {verb} an email to {subject}. "
                "It's in your Authority Queue — read it over, make sure the tone "
                "and content are right, then approve to send."
            )
        if "sms" in tt or "whatsapp" in tt:
            msg_channel = "WhatsApp message" if "whatsapp" in tt else "text message"
            return (
                f"{header}I've {verb} a {msg_channel} to {subject}. "
                "It's in your Authority Queue — give it a quick read "
                "and approve when it looks good."
            )
        if "calendar" in tt:
            title = (execution_params or {}).get("title", "an event")
            return (
                f"{header}I've {verb} a calendar event — \"{title}\". "
                "It's in your Authority Queue — check the time and details, "
                "then approve to lock it in."
            )
        if "quote" in tt:
            return (
                f"{header}I've {verb} a quote for {subject}{amount_str}. "
                "It's in your Authority Queue — review the line items and pricing, "
                "and approve when everything looks right."
            )
        if "payroll" in tt:
            return (
                f"{header}I've {verb} a payroll run{amount_str}. "
                "It's in your Authority Queue — review the amounts and employee details "
                "carefully, then approve when you're confident it's correct."
            )
        if "payment" in tt or "transfer" in tt:
            return (
                f"{header}I've {verb} a payment{amount_str}. "
                "It's in your Authority Queue — verify the recipient and amount, "
                "then approve when you're ready to release the funds."
            )

        # Catch-all pending — still conversational
        risk_note = (
            " This is a red-tier action, so I'll need your video presence to proceed."
            if risk_tier == "red" else ""
        )
        return (
            f"{header}I've prepared that for you and it's waiting in your Authority Queue.{risk_note} "
            "Take a look and approve or deny when you're ready."
        )

    # ── SUCCESS — action completed ───────────────────────────────────────
    if outcome == "success":

        # Invoice
        if "invoice" in tt:
            er = execution_result or {}
            if er.get("authority_queue_id") or (er.get("data", {}).get("status") == "draft"):
                preview_url = (er.get("data") or {}).get("hosted_invoice_url")
                preview_note = f" Here's a preview link: {preview_url}" if preview_url else ""
                return (
                    f"{header}I've put together a draft invoice for {subject}"
                    f"{_money_from_params(execution_params)}. "
                    "It's in your Authority Queue — look it over and approve "
                    f"when you're ready to send it out.{preview_note}"
                )
            return (
                f"{header}The invoice for {subject}{_money_from_params(execution_params)} "
                "has been sent. They should have it in their inbox now."
            )

        # Quote
        if "quote" in tt:
            er = execution_result or {}
            if er.get("authority_queue_id") or (er.get("data", {}).get("status") in ("draft", "open")):
                amount_total = (er.get("data") or {}).get("amount_total", 0)
                amount_note = f" for {_payout_amount_str(amount_total)}" if amount_total else ""
                return (
                    f"{header}I've drafted a quote for {subject}{amount_note}. "
                    "It's in your Authority Queue — review the line items and pricing, "
                    "then approve to accept it."
                )
            return (
                f"{header}The quote for {subject} has been accepted. "
                "Stripe will generate an invoice from it automatically."
            )

        # Payout (list)
        if "payout" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            payouts = data.get("payouts", [])
            if payouts:
                lines = []
                for p in payouts[:5]:
                    amt = p.get("amount", 0)
                    cur = p.get("currency") or "usd"
                    status = p.get("status", "unknown")
                    arrival = p.get("arrival_date")
                    amt_str = _payout_amount_str(amt, cur)
                    date_str = _format_timestamp(arrival)
                    if date_str:
                        lines.append(f"{amt_str} — {status}, arrives {date_str}")
                    else:
                        lines.append(f"{amt_str} — {status}")
                summary = "; ".join(lines)
                count = data.get("count", len(payouts))
                if count > 5:
                    return f"{header}You've got {count} payouts on record. Here are the most recent five: {summary}."
                elif count == 1:
                    return f"{header}You have one payout: {summary}."
                else:
                    return f"{header}Here are your {count} payouts: {summary}."

            # Single payout detail (payout.read)
            if data.get("payout_id"):
                amt = data.get("amount", 0)
                cur = data.get("currency") or "usd"
                status = data.get("status", "unknown")
                arrival = data.get("arrival_date")
                amt_str = _payout_amount_str(amt, cur)
                date_str = _format_timestamp(arrival)

                if status == "paid" and date_str:
                    return f"{header}That payout of {amt_str} landed in your account on {date_str}."
                if status == "paid":
                    return f"{header}That payout of {amt_str} has been deposited."
                if status in ("pending", "in_transit") and date_str:
                    status_word = "on its way" if status == "in_transit" else "pending"
                    return f"{header}Your payout of {amt_str} is {status_word} — it should arrive by {date_str}."
                if status in ("pending", "in_transit"):
                    return f"{header}Your payout of {amt_str} is {status}. I'll keep an eye on it."
                if status == "failed":
                    reason = data.get("failure_message") or data.get("failure_code") or "an unknown issue"
                    return (
                        f"{header}That payout of {amt_str} didn't go through — {reason}. "
                        "You may want to check your bank details in Stripe."
                    )
                if status == "canceled":
                    return f"{header}That payout of {amt_str} was canceled before it went out."
                if date_str:
                    return f"{header}Payout of {amt_str} — currently {status}, expected by {date_str}."
                return f"{header}Payout of {amt_str} — currently {status}."

            return f"{header}I checked, but there aren't any payouts matching that criteria right now."

        # Email
        if "email" in tt:
            return (
                f"{header}Your email to {subject} just went out. "
                "I'll keep an eye on the thread in case they reply."
            )

        # Calendar
        if "calendar" in tt:
            title = (execution_params or {}).get("title", "your event")
            return f"{header}All set — \"{title}\" is on your calendar now."

        # Search / Research
        if "search" in tt or "research" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            count = data.get("result_count") or data.get("count")
            if isinstance(count, int) and count > 0:
                return f"{header}I found {count} results for you. Here's what came up."
            return f"{header}I've pulled together some results — take a look and let me know if you need me to dig deeper."

        # Contract
        if "contract" in tt:
            return (
                f"{header}The contract has been sent over to {subject} for signature. "
                "I'll let you know as soon as they sign."
            )

        # Meeting / Conference
        if "meeting" in tt or "conference" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            room_name = data.get("room_name") or data.get("name")
            join_code = data.get("join_code")
            if join_code:
                return (
                    f"{header}Your meeting room is ready — join code is {join_code}. "
                    "Share it with your participants whenever you're set."
                )
            if room_name:
                return f"{header}Your meeting room \"{room_name}\" is live and ready to go."
            return f"{header}Your meeting room is set up. You're good to go whenever you're ready."

        # Finance snapshot
        if "finance" in tt or "snapshot" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            if data and not data.get("stub"):
                cash = data.get("cash_position_cents", 0)
                revenue = data.get("revenue_cents", 0)
                expenses = data.get("expenses_cents", 0)
                net = data.get("net_income_cents", 0)
                parts = [f"{header}Here's where things stand financially"]
                if cash:
                    parts.append(f"you've got ${cash / 100:,.2f} cash on hand")
                if revenue:
                    parts.append(f"${revenue / 100:,.2f} in revenue this period")
                if expenses:
                    parts.append(f"${expenses / 100:,.2f} in expenses")
                if net:
                    sign = "up" if net > 0 else "down"
                    parts.append(f"net income is {sign} ${abs(net) / 100:,.2f}")
                sources = data.get("data_source", "")
                if sources and sources != "stub":
                    parts.append(f"pulled from {sources}")
                if len(parts) > 1:
                    return parts[0] + " — " + ", ".join(parts[1:]) + "."
                return parts[0] + "."
            return (
                f"{header}I don't have financial data to show you yet. "
                "Once your providers are connected in Settings, I'll have real numbers for you."
            )

        # Financial exceptions
        if "exception" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            count = data.get("exception_count", len(data.get("exceptions", [])))
            if count > 0:
                return (
                    f"{header}Heads up — I found {count} financial "
                    f"exception{'s' if count != 1 else ''} that need your attention. "
                    "Take a look in the Finance Hub when you get a chance."
                )
            return f"{header}Everything looks clean on the financial side — no exceptions flagged."

        # Financial health / reports
        if "health" in tt or "report" in tt:
            return (
                f"{header}I've put together a financial analysis for you. "
                "The details are in the Finance Hub — take a look when you have a minute."
            )

        # Budget
        if "budget" in tt:
            return (
                f"{header}I've drafted a budget adjustment proposal based on what I'm seeing. "
                "It's in your Authority Queue for review."
            )

        # Phone / Call
        if "call" in tt or "phone" in tt or "telephony" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            call_status = data.get("status", "")
            if call_status == "queued":
                return f"{header}The call is queued up and going out now."
            if call_status in ("ringing", "in-progress"):
                return f"{header}The call is ringing now."
            return f"{header}I've placed that call for you."

        # Domain / DNS
        if "domain" in tt or "dns" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            domain_name = data.get("domain") or data.get("domain_name")
            if domain_name:
                return f"{header}The domain setup for {domain_name} is in progress. I'll monitor the DNS propagation."
            return f"{header}Domain operation completed. The changes should propagate shortly."

        # Mailbox / Email setup
        if "mailbox" in tt or "mail_ops" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            email_addr = data.get("email") or data.get("address")
            if email_addr:
                return f"{header}The mailbox {email_addr} is set up and ready to use."
            return f"{header}Your mailbox is configured. You should be able to send and receive now."

        # Document generation (Tec)
        if "document" in tt or "pdf" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            doc_name = data.get("title") or data.get("filename")
            if doc_name:
                return f"{header}Your document \"{doc_name}\" is ready. You can download it from the Documents section."
            return f"{header}Your document is ready for download."

        # Delegation / A2A
        if "delegation" in tt or "a2a" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            to_agent = data.get("to_agent", "the right specialist")
            return f"{header}I've handed this off to {to_agent} — they'll take it from here and I'll keep you posted."

        # Bookkeeping / QuickBooks
        if "books" in tt or "quickbooks" in tt or "accounting" in tt:
            return f"{header}The books have been updated. Everything is synced."

        # Customer operations
        if "customer" in tt:
            er = execution_result or {}
            data = er.get("data", {}) if isinstance(er, dict) else {}
            customer_name = data.get("name") or subject
            if customer_name:
                return f"{header}{customer_name}'s profile has been updated in Stripe."
            return f"{header}The customer record has been updated."

        # Catch-all success — still conversational, never robotic
        return f"{header}All set — that's taken care of."

    # ── FAILED — never expose raw error details (enterprise security) ────
    if outcome == "failed":
        er = execution_result or {}
        err_text = str(er.get("error") or er.get("reason_code") or "").upper()

        if "MODEL_UNAVAILABLE" in err_text:
            return (
                f"{header}I hit a temporary snag — my AI engine is briefly unavailable. "
                "Give it a moment and try again."
            )
        if "CHECKPOINTER_UNAVAILABLE" in err_text:
            return (
                f"{header}I lost access to the conversation memory for a moment there. "
                "Try that again and it should work."
            )
        if "TIMEOUT" in err_text:
            return (
                f"{header}That took longer than expected and timed out. "
                "Want me to try again, maybe with something more specific?"
            )
        if "AUTH" in err_text or "INVALID_KEY" in err_text:
            return (
                f"{header}I couldn't connect to one of the services — looks like a provider "
                "credential might be missing or expired. Check your connections in Settings."
            )
        if "RATE_LIMIT" in err_text:
            return (
                f"{header}I'm getting rate-limited by one of the providers right now. "
                "Give it a minute and I'll try again."
            )
        if "PROVIDER_ALL_FAILED" in err_text or "ALL PROVIDERS FAILED" in err_text:
            return (
                f"{header}None of the research providers came back with results this time. "
                "Try narrowing the query, or I can give it another shot in a minute."
            )
        if "NOT_FOUND" in err_text:
            return (
                f"{header}I couldn't find what I was looking for — it may have been "
                "deleted or the ID might be wrong. Can you double-check?"
            )
        if "INSUFFICIENT_FUNDS" in err_text:
            return (
                f"{header}That didn't go through — the account doesn't have enough funds "
                "to cover it right now."
            )
        if "INPUT_MISSING" in err_text or "INPUT_INVALID" in err_text:
            return (
                f"{header}I'm missing some information I need for that. "
                "Can you give me a few more details?"
            )

        # Catch-all failure — still warm, never robotic
        return (
            f"{header}Something went wrong on that one. "
            "Want me to give it another try, or take a different approach?"
        )

    # ── DENIED — policy blocked ──────────────────────────────────────────
    if outcome == "denied":
        return (
            f"{header}I can't do that one — it's outside what your security policy allows. "
            "If you think this should be permitted, you can update the policy in Settings."
        )

    # ── Catch-all — should rarely hit ────────────────────────────────────
    return f"{header}All set — I've taken care of that for you."
