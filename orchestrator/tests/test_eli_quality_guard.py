from __future__ import annotations

from aspire_orchestrator.services.eli_deliverability_monitor import evaluate_deliverability
from aspire_orchestrator.services.eli_quality_guard import evaluate_email_quality


class TestEliQualityGuard:
    def test_rejects_weak_draft(self) -> None:
        report = evaluate_email_quality(
            payload={
                "subject": "Hi",
                "body_text": "Checking in.",
            },
            mode="draft",
        )
        assert report.passed is False
        assert report.score < 78
        assert any("subject too short" in v for v in report.violations)

    def test_accepts_strong_draft(self) -> None:
        report = evaluate_email_quality(
            payload={
                "subject": "Invoice 1047 payment follow-up",
                "body_text": (
                    "Hi Sarah,\n\n"
                    "I wanted to follow up on invoice 1047 that was due last week. "
                    "Please confirm if payment is scheduled for this week, and if there is any issue "
                    "I can help resolve. If useful, I can resend the invoice PDF and payment link today.\n\n"
                    "Best,\nEli\nAspire Inbox Desk"
                ),
            },
            mode="draft",
        )
        assert report.passed is True
        assert report.score >= 78

    def test_rejects_emoji_and_slang(self) -> None:
        report = evaluate_email_quality(
            payload={
                "subject": "Quick update on proposal",
                "body_text": (
                    "Hi Sarah,\n\n"
                    "Just circling back lol 😀 can you approve this today.\n\n"
                    "Best,\nEli\nAspire Inbox Desk"
                ),
            },
            mode="draft",
        )
        assert report.passed is False
        assert any("contains emoji" in v for v in report.violations)


class TestEliDeliverabilityMonitor:
    def test_blocks_high_spam_rate(self) -> None:
        status = evaluate_deliverability({"spam_rate": 0.35})
        assert status.level == "blocked"
        assert any("spam_rate" in reason for reason in status.reasons)

    def test_warns_without_tls(self) -> None:
        status = evaluate_deliverability({"spam_rate": 0.02, "tls_enabled": False})
        assert status.level == "warning"
        assert any("tls" in reason for reason in status.reasons)
