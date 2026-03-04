from __future__ import annotations

import re

from aspire_orchestrator.services.eli_email_param_helpers import (
    body_text_to_html,
    extract_emails,
    extract_subject_hint,
    naturalize_email_body,
    synthesize_body_text,
)


class TestEliEmailParamHelpers:
    def test_extract_emails(self) -> None:
        text = "draft email to sarah@northstarco.com from ceo@aspireos.app"
        emails = extract_emails(text)
        assert "sarah@northstarco.com" in emails
        assert "ceo@aspireos.app" in emails

    def test_extract_subject_hint(self) -> None:
        text = "subject should be Project Timeline Follow-Up and keep it concise"
        subject = extract_subject_hint(text)
        assert subject == "Project Timeline Follow-Up"

    def test_synthesize_body_has_cta_and_min_length(self) -> None:
        body = synthesize_body_text(
            to_email="sarah@northstarco.com",
            subject="Project Timeline Follow-Up",
            utterance=(
                "mention milestone 2 is complete, ask for approval by Friday, "
                "and propose a 20-minute call next Tuesday at 10 AM ET"
            ),
        )
        word_count = len(re.findall(r"\b[\w'-]+\b", body))
        assert word_count >= 30
        assert "confirm approval by friday" in body.lower()
        assert "let me know" in body.lower()

    def test_body_text_to_html(self) -> None:
        html = body_text_to_html("Hi Sarah,\n\nPlease confirm by Friday.\n\nBest,\nAspire Team")
        assert "<p>Hi Sarah," in html
        assert "<p>Please confirm by Friday." in html

    def test_naturalize_email_body(self) -> None:
        raw = (
            "Please provide your approval by Friday, 2026-03-06 (end of day). "
            "I can also do a 20-minute call on Tuesday, 2026-03-10 at 10:00 AM ET "
            "(2026-03-10T10:00:00-04:00). Please confirm your approval or your availability for the call."
        )
        cleaned = naturalize_email_body(raw)
        assert "Could you confirm by Friday end of day" in cleaned
        assert "2026-03-10T10:00:00-04:00" not in cleaned
