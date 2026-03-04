from __future__ import annotations

import pytest

from aspire_orchestrator.services.intent_classifier import IntentClassifier


@pytest.mark.asyncio
async def test_eli_tweak_routes_to_email_draft() -> None:
    clf = IntentClassifier()
    result = await clf.classify(
        "make it warmer and shorter",
        context={"current_agent": "eli"},
    )
    assert result.action_type == "email.draft"
    assert result.skill_pack == "eli_inbox"
    assert result.requires_clarification is False
