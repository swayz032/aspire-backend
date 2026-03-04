from __future__ import annotations

import re

from aspire_orchestrator.services.eli_email_param_helpers import (
    apply_email_tweaks,
    body_text_to_html,
    extract_emails,
    extract_labeled_email,
    extract_subject_hint,
    infer_subject_from_utterance,
    is_email_tweak_request,
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

    def test_extract_subject_hint_strips_instruction_tail(self) -> None:
        text = (
            "Subject: Commercial Roofing Proposal - Harbor Blvd Facility "
            "Include scope, timeline, permit compliance, and warranty"
        )
        subject = extract_subject_hint(text)
        assert subject == "Commercial Roofing Proposal - Harbor Blvd Facility"

    def test_extract_labeled_email(self) -> None:
        text = "Recipient: procurement@coastalwarehousing.com Sender: bids@skyline-roofing.com"
        assert extract_labeled_email(text, "recipient") == "procurement@coastalwarehousing.com"
        assert extract_labeled_email(text, "sender") == "bids@skyline-roofing.com"

    def test_infer_subject_from_utterance_roofing(self) -> None:
        subject = infer_subject_from_utterance(
            "write a binding roofing proposal for Harbor Blvd Facility with warranty details"
        )
        assert "binding proposal" in subject.lower()
        assert "harbor blvd facility" in subject.lower()

    def test_infer_subject_from_utterance_fallback(self) -> None:
        assert infer_subject_from_utterance("please send a quick update") == "Quick Follow-Up"

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
        assert "best,\neli\naspire inbox desk" in body.lower()

    def test_synthesize_binding_proposal_email(self) -> None:
        body = synthesize_body_text(
            to_email="procurement@coastalwarehousing.com",
            subject="Commercial Roofing Proposal - Harbor Blvd Facility",
            utterance=(
                "draft a binding roofing proposal and include scope, materials, timeline, "
                "permit compliance, three pricing options, payment schedule, and warranty"
            ),
            from_address="bids@skyline-roofing.com",
        )
        lower = body.lower()
        assert "binding proposal" in lower
        assert "scope of work" in lower
        assert "pricing options" in lower
        assert "payment schedule" in lower
        assert "acceptance:" in lower
        assert "best,\nbids\naspire inbox desk" in lower

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

    def test_tweak_detection_and_apply(self) -> None:
        assert is_email_tweak_request("make it warmer and shorter") is True
        subject, body = apply_email_tweaks(
            subject="Project Timeline Follow-Up",
            body_text=(
                "Hi Sarah,\n\nCould you confirm by Friday end of day?\n\n"
                "I can also do a 20-minute call on Tuesday at 10:00 AM ET.\n\nThanks,\nAspire Team"
            ),
            utterance="make it warmer and shorter and add we can move quickly once approved",
        )
        assert subject == "Project Timeline Follow-Up"
        assert "Hi Sarah" in body
        assert "Would you mind confirming" in body
        assert "move quickly once approved" in body.lower()
