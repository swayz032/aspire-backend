"""Eli email parameter helpers for natural-language draft/send prompts."""

from __future__ import annotations

import re


def extract_emails(text: str) -> list[str]:
    return re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text or "")


def display_name_from_email(address: str) -> str:
    local = (address.split("@", 1)[0] if "@" in address else address).strip().replace(".", " ")
    parts = [p for p in re.split(r"[_\-\s]+", local) if p]
    if not parts:
        return "there"
    return " ".join(p[:1].upper() + p[1:] for p in parts[:2])


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


def _sentence(text: str) -> str:
    cleaned = (text or "").strip(" .")
    if not cleaned:
        return ""
    normalized = cleaned[:1].upper() + cleaned[1:]
    return normalized if normalized.endswith((".", "!", "?")) else f"{normalized}."


def synthesize_body_text(*, to_email: str, subject: str, utterance: str) -> str:
    contact = display_name_from_email(to_email)
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
        _sentence("Please reply when you can so we can keep this moving"),
        "",
        "Best,",
        "Aspire Team",
    ]
    body = "\n".join(lines).strip()
    if len(re.findall(r"\b[\w'-]+\b", body)) < 30:
        body = (
            f"Hi {contact},\n\n"
            f"{_sentence(f'I wanted to share a quick update: {mention}')} "
            f"{_sentence(ask_norm)} "
            f"If helpful, I can share additional context on {subject.lower()} and next steps.\n\n"
            "Please reply when you can so we can keep this moving.\n\n"
            "Best,\nAspire Team"
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
