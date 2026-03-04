"""Eli email parameter helpers for natural-language draft/send prompts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re

DEFAULT_SIGNOFF = "Best,\nEli\nAspire Inbox Desk"


def extract_emails(text: str) -> list[str]:
    return re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text or "")


def extract_labeled_email(text: str, label: str) -> str | None:
    pattern = rf"\b{re.escape(label)}\s*[:=]\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{{2,}})\b"
    m = re.search(pattern, text or "", re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


def display_name_from_email(address: str) -> str:
    local = (address.split("@", 1)[0] if "@" in address else address).strip().replace(".", " ")
    parts = [p for p in re.split(r"[_\-\s]+", local) if p]
    if not parts:
        return "there"
    return " ".join(p[:1].upper() + p[1:] for p in parts[:2])


def signoff_from_sender(from_address: str | None) -> str:
    if not from_address:
        return DEFAULT_SIGNOFF
    sender_name = display_name_from_email(from_address)
    return f"Best,\n{sender_name}\nAspire Inbox Desk"


def extract_subject_hint(utterance: str) -> str | None:
    patterns = [
        r"\bsubject(?:\s+should\s+be|\s*[:=])\s*[\"']?([^\"'\n\r]{4,120})[\"']?",
        r"\bwith\s+subject\s*[\"']?([^\"'\n\r]{4,120})[\"']?",
    ]
    for pattern in patterns:
        m = re.search(pattern, utterance, re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .")
            if value:
                lower = value.lower()
                cut_markers = (
                    " mention ",
                    " ask ",
                    " propose ",
                    " and keep ",
                    ". mention",
                    ". ask",
                    ". propose",
                )
                cut_idx = len(value)
                for marker in cut_markers:
                    idx = lower.find(marker)
                    if idx >= 0:
                        cut_idx = min(cut_idx, idx)
                value = value[:cut_idx].strip(" .,:;")
                return value
    return None


def extract_instruction_clause(utterance: str, verb: str) -> str | None:
    pattern = rf"\b{verb}\s+(.+?)(?:[.;]|,\s*(?:ask|propose|mention|tell|keep)\b| and (?:ask|propose|mention|tell|keep)\b|$)"
    m = re.search(pattern, utterance, re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip(" .")
    return value or None


def _proposal_requested(utterance: str, subject: str) -> bool:
    lower = f"{utterance} {subject}".lower()
    signals = (
        "proposal",
        "binding",
        "bid",
        "scope",
        "pricing option",
        "warranty",
        "permit",
        "acceptance",
    )
    return any(s in lower for s in signals)


def _contains(text: str, *needles: str) -> bool:
    lower = (text or "").lower()
    return any(n in lower for n in needles)


def _proposal_body(contact: str, subject: str, utterance: str, signoff: str) -> str:
    target = "this project"
    m = re.search(r"\bfor\s+(.+?)(?:[.?!]|$)", utterance, re.IGNORECASE)
    if m:
        target = m.group(1).strip(" .")

    deadline = (datetime.now(UTC) + timedelta(days=7)).strftime("%A, %B %d, %Y")
    include_three_options = _contains(utterance, "three pricing options", "3 pricing options", "price options")
    mention_permit = _contains(utterance, "permit", "compliance")
    mention_warranty = _contains(utterance, "warranty")
    mention_timeline = _contains(utterance, "timeline", "mobilization", "start date")

    lines = [
        f"Hi {contact},",
        "",
        f"Following up on your request, here is our binding proposal for {target}.",
        "",
        "Scope of work:",
        "- Full tear-off and disposal of existing roofing system where required",
        "- Deck inspection, moisture remediation, and substrate prep",
        "- Installation of the specified commercial roofing assembly with QA checkpoints",
    ]
    if mention_timeline:
        lines += [
            "",
            "Execution timeline:",
            "- Mobilization within 7 business days of notice to proceed",
            "- Estimated onsite duration: 10-15 business days (weather dependent)",
            "- Daily progress reporting and final closeout package at completion",
        ]
    if mention_permit:
        lines += [
            "",
            "Permits and compliance:",
            "- We handle permit submission/coordination and required inspections",
            "- Work will be executed to manufacturer specs and local code requirements",
        ]
    if include_three_options:
        lines += [
            "",
            "Pricing options:",
            "- Option A (Base): code-compliant system with standard manufacturer warranty",
            "- Option B (Performance): upgraded insulation and extended coverage",
            "- Option C (Premium): enhanced system with highest lifecycle durability profile",
            "",
            "Payment schedule:",
            "- 30% mobilization deposit",
            "- 40% at material delivery and dry-in milestone",
            "- 30% at substantial completion and sign-off",
        ]
    if mention_warranty:
        lines += [
            "",
            "Warranty:",
            "- Manufacturer material warranty plus workmanship warranty from our team",
            "- Final warranty terms are tied to selected option and manufacturer approval",
        ]

    lines += [
        "",
        f"Acceptance: if this aligns with your requirements, please reply with written approval by {deadline}, and I will issue the final execution package the same day.",
        "",
        signoff,
    ]
    return "\n".join(lines).strip()


def _sentence(text: str) -> str:
    cleaned = (text or "").strip(" .")
    if not cleaned:
        return ""
    normalized = cleaned[:1].upper() + cleaned[1:]
    return normalized if normalized.endswith((".", "!", "?")) else f"{normalized}."


def synthesize_body_text(*, to_email: str, subject: str, utterance: str, from_address: str | None = None) -> str:
    signoff = signoff_from_sender(from_address)
    contact = display_name_from_email(to_email)
    if _proposal_requested(utterance, subject):
        return _proposal_body(contact, subject, utterance, signoff)

    mention = (
        extract_instruction_clause(utterance, "mention")
        or extract_instruction_clause(utterance, "tell")
        or "I wanted to share a quick update."
    )
    ask = extract_instruction_clause(utterance, "ask") or "Please let me know your confirmation."
    propose = extract_instruction_clause(utterance, "propose")

    ask_norm = ask.strip()
    if ask_norm.lower().startswith("for "):
        ask_norm = f"confirm {ask_norm[4:].strip()}"
    if not ask_norm.lower().startswith(("please ", "can ", "could ", "kindly ")):
        ask_norm = f"Please {ask_norm}"

    body_lines = [
        _sentence(f"I wanted to share a quick update: {mention}"),
        _sentence(ask_norm),
    ]
    if propose:
        body_lines.append(_sentence(f"I can also do {propose} if that works for you"))

    lines = [
        f"Hi {contact},",
        "",
        *[line for line in body_lines if line],
        _sentence("Let me know what works best on your side"),
        "",
        signoff,
    ]
    body = "\n".join(lines).strip()
    if len(re.findall(r"\b[\w'-]+\b", body)) < 30:
        body = (
            f"Hi {contact},\n\n"
            f"{_sentence(f'I wanted to share a quick update: {mention}')} "
            f"{_sentence(ask_norm)} "
            f"If helpful, I can share additional context on {subject.lower()} and next steps.\n\n"
            "Let me know what works best on your side.\n\n"
            f"{signoff}"
        )
    return body


def body_text_to_html(body_text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", body_text or "") if p.strip()]
    if not paragraphs:
        return ""
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def naturalize_email_body(body_text: str) -> str:
    """Convert machine-like date/time or phrasing into natural email language."""
    text = body_text or ""

    # Remove explicit ISO timestamps often appended by extraction models.
    text = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}T[^)]+\)", "", text)

    # Humanize explicit machine date renderings where weekday is already present.
    text = re.sub(
        r"\bby ([A-Za-z]+), \d{4}-\d{2}-\d{2} \(end of day\)",
        r"by \1 end of day",
        text,
    )
    text = re.sub(
        r"\bon ([A-Za-z]+), \d{4}-\d{2}-\d{2} at ([0-9: ]+[AP]M ET)\b",
        r"on \1 at \2",
        text,
    )

    # De-robotify overly formal constructions.
    text = text.replace("Please provide your approval", "Could you confirm")
    text = text.replace("Please confirm your approval or your availability for the call.", "Could you confirm if that timing works for you?")
    text = text.replace("Best regards,", "Thanks,")

    # Normalize spacing and preserve paragraph breaks.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_email_tweak_request(utterance: str) -> bool:
    lower = (utterance or "").lower()
    tweak_markers = (
        "tweak",
        "revise",
        "rewrite",
        "make it",
        "change it",
        "update it",
        "adjust",
        "shorter",
        "longer",
        "more friendly",
        "warmer",
        "more formal",
        "less formal",
    )
    return any(marker in lower for marker in tweak_markers)


def apply_email_tweaks(*, subject: str, body_text: str, utterance: str) -> tuple[str, str]:
    """Apply simple conversational tweak directives to an existing draft."""
    lower = (utterance or "").lower()
    updated_subject = subject or ""
    updated_body = body_text or ""

    if "shorter" in lower:
        lines = [l.strip() for l in updated_body.splitlines() if l.strip()]
        if len(lines) > 5:
            updated_body = "\n\n".join(lines[:4] + [lines[-2], lines[-1]])

    if "more friendly" in lower or "warmer" in lower:
        updated_body = updated_body.replace("Hi ", "Hey ")
        updated_body = updated_body.replace("Could you confirm", "Would you mind confirming")
        updated_body = updated_body.replace("Thanks,", "Appreciate it,")

    if "more formal" in lower:
        updated_body = updated_body.replace("Hey ", "Hello ")
        updated_body = updated_body.replace("Would you mind confirming", "Please confirm")
        updated_body = updated_body.replace("Appreciate it,", "Regards,")

    if "less formal" in lower:
        updated_body = updated_body.replace("Hello ", "Hi ")
        updated_body = updated_body.replace("Please confirm", "Can you confirm")

    # Subject tweak patterns: "subject to X", "change subject to X"
    m = re.search(r"\b(?:change\s+)?subject\s+(?:to|as)\s+(.+)$", utterance, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip(" .")
        if candidate:
            updated_subject = candidate

    # Add sentence pattern: "add ...".
    m_add = re.search(r"\badd\s+(.+)$", utterance, re.IGNORECASE)
    if m_add:
        addition = m_add.group(1).strip(" .")
        if addition and addition.lower() not in updated_body.lower():
            updated_body = f"{updated_body.rstrip()}\n\n{addition[:240].rstrip('.') }."

    updated_body = naturalize_email_body(updated_body)
    return updated_subject, updated_body
