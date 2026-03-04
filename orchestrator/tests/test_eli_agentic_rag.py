from __future__ import annotations

import pytest

from aspire_orchestrator.services.eli_agentic_rag import run_eli_agentic_rag


class _FakeRagResult:
    def __init__(self, chunks):
        self.chunks = chunks


class _FakeCommService:
    async def retrieve(self, query: str, suite_id: str | None = None):  # noqa: ARG002
        return _FakeRagResult(
            [
                {
                    "domain": "email_best_practices",
                    "chunk_type": "guideline",
                    "content": "Open with purpose and keep subject lines concise.",
                },
            ]
        )

    def assemble_rag_context(self, result: _FakeRagResult) -> str:  # noqa: ARG002
        return "Open with purpose. Keep professional email structure."


@pytest.mark.asyncio
async def test_agentic_rag_primary_mode(monkeypatch) -> None:
    from aspire_orchestrator.services import eli_agentic_rag as mod

    monkeypatch.setattr(mod, "get_communication_retrieval_service", lambda: _FakeCommService())

    params, meta = await run_eli_agentic_rag(
        task_type="email.draft",
        assigned_agent="eli",
        utterance="draft a follow up email",
        suite_id="suite-123",
        params={
            "to": "sarah@northstarco.com",
            "from_address": "eli@aspireos.app",
            "subject": "Following up on proposal status and next steps with additional details",
            "body_text": "Hope this email finds you well. Could you confirm your timing?",
        },
    )

    assert meta["eli_rag_status"] == "primary"
    assert meta["eli_fallback_mode"] is False
    assert "email_best_practices" in meta["eli_rag_sources"]
    assert "Hi Sarah" in params["body_text"]
    assert "Best,\nEli\nAspire Inbox Desk" in params["body_text"]


@pytest.mark.asyncio
async def test_agentic_rag_fallback_when_retrieval_fails(monkeypatch) -> None:
    from aspire_orchestrator.services import eli_agentic_rag as mod

    def _raise():
        raise RuntimeError("rpc down")

    monkeypatch.setattr(mod, "get_communication_retrieval_service", _raise)

    params, meta = await run_eli_agentic_rag(
        task_type="email.send",
        assigned_agent="eli",
        utterance="send reminder email",
        suite_id="suite-123",
        params={
            "to": "client@example.com",
            "from_address": "eli@aspireos.app",
            "subject": "Reminder",
            "body_text": "Please confirm",
        },
    )

    assert meta["eli_rag_status"] == "offline"
    assert meta["eli_fallback_mode"] is True
    assert "Best,\nEli\nAspire Inbox Desk" in params["body_text"]
    assert params.get("body_html", "").startswith("<p>")


@pytest.mark.asyncio
async def test_agentic_rag_not_applicable_for_non_eli_task() -> None:
    params, meta = await run_eli_agentic_rag(
        task_type="invoice.create",
        assigned_agent="quinn",
        utterance="create invoice",
        suite_id="suite-123",
        params={"subject": "x"},
    )

    assert params["subject"] == "x"
    assert meta["eli_rag_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_agentic_rag_builds_advanced_proposal_body(monkeypatch) -> None:
    from aspire_orchestrator.services import eli_agentic_rag as mod

    monkeypatch.setattr(mod, "get_communication_retrieval_service", lambda: _FakeCommService())

    params, meta = await run_eli_agentic_rag(
        task_type="email.draft",
        assigned_agent="eli",
        utterance=(
            "draft a binding roofing proposal and include scope, materials, timeline, "
            "permit compliance, three pricing options, payment schedule, and warranty"
        ),
        suite_id="suite-123",
        params={
            "to": "procurement@coastalwarehousing.com",
            "from_address": "bids@skyline-roofing.com",
            "subject": "Commercial Roofing Proposal - Harbor Blvd Facility",
            "body_text": "Please confirm",
        },
    )

    body = params["body_text"].lower()
    assert "binding proposal" in body
    assert "pricing options:" in body
    assert "payment schedule:" in body
    assert "warranty terms:" in body
    assert meta["eli_rag_status"] == "primary"
