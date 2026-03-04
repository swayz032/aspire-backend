from __future__ import annotations

import re

from aspire_orchestrator.services.eli_email_param_helpers import (
    body_text_to_html,
    extract_emails,
    extract_subject_hint,
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
        assert subject == "Project Timeline Follow-Up and keep it concise"

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
        assert "approval by friday" in body.lower()
        assert "reply" in body.lower()

    def test_body_text_to_html(self) -> None:
        html = body_text_to_html("Hi Sarah,\n\nPlease confirm by Friday.\n\nBest,\nAspire Team")
        assert "<p>Hi Sarah," in html
        assert "<p>Please confirm by Friday." in html
