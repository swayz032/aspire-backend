"""Tests for Clara RAG integration — Each skill pack method with RAG enabled/disabled.

Verifies graceful degradation: if RAG fails, existing behavior preserved.
All PandaDoc, Supabase, and RAG calls mocked.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_retrieval_service():
    """Mock the legal retrieval service singleton."""
    from aspire_orchestrator.services.legal_retrieval_service import RetrievalResult

    svc = MagicMock()
    svc.retrieve = AsyncMock(
        return_value=RetrievalResult(
            chunks=[
                {
                    "id": "chunk-rag-1",
                    "content": "Force majeure releases parties from obligations.",
                    "domain": "contract_law",
                    "subdomain": "clauses",
                    "chunk_type": "clause",
                    "template_key": None,
                    "template_lane": None,
                    "jurisdiction_state": None,
                    "confidence_score": 1.0,
                    "attorney_reviewed": False,
                    "vector_similarity": 0.92,
                    "text_rank": 0.5,
                    "combined_score": 0.79,
                }
            ],
            query="test query",
            cache_hit=False,
            receipt_id="receipt-rag-001",
        )
    )
    svc.assemble_rag_context = MagicMock(
        return_value="--- RELEVANT LEGAL KNOWLEDGE (Clara RAG) ---\n[Knowledge 1/1] test context\n--- END LEGAL KNOWLEDGE ---"
    )
    return svc


@pytest.fixture
def mock_retrieval_service_failure():
    """Mock retrieval service that always raises."""
    svc = MagicMock()
    svc.retrieve = AsyncMock(side_effect=Exception("RAG service unavailable"))
    svc.assemble_rag_context = MagicMock(return_value="")
    return svc


def _make_clara_context():
    """Create a ClaraContext for testing."""
    from aspire_orchestrator.skillpacks.clara_legal import ClaraContext
    return ClaraContext(
        suite_id="suite-test-1",
        office_id="office-test-1",
        correlation_id="corr-test-001",
        capability_token_id="tok-test-1",
    )


def _make_agent_context():
    """Create an AgentContext for testing."""
    from aspire_orchestrator.services.agent_sdk_base import AgentContext
    return AgentContext(
        suite_id="suite-test-1",
        office_id="office-test-1",
        correlation_id="corr-test-001",
        actor_id="test-user",
    )


def _make_tool_result(**overrides):
    """Create a ToolExecutionResult with correct field names."""
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome

    defaults = dict(
        outcome=Outcome.SUCCESS,
        tool_id="pandadoc.templates.list",
        data={"templates": [{"id": "t1", "name": "NDA"}]},
        error=None,
    )
    defaults.update(overrides)
    return ToolExecutionResult(**defaults)


@pytest.fixture
def mock_tool_executor():
    """Mock tool_executor.execute_tool to simulate PandaDoc responses."""
    with patch(
        "aspire_orchestrator.skillpacks.clara_legal.execute_tool",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = _make_tool_result()
        yield mock


@pytest.fixture
def clara_instance():
    """Create a ClaraLegalSkillPack instance."""
    from aspire_orchestrator.skillpacks.clara_legal import ClaraLegalSkillPack
    return ClaraLegalSkillPack()


@pytest.fixture
def enhanced_clara_instance():
    """Create an EnhancedClaraLegal instance."""
    from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal
    return EnhancedClaraLegal()


# ---------------------------------------------------------------------------
# Tests: browse_templates with RAG
# ---------------------------------------------------------------------------


class TestBrowseTemplatesRAG:
    async def test_rag_enriches_template_search(
        self, clara_instance, mock_tool_executor, mock_retrieval_service
    ):
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service,
        ):
            result = await clara_instance.browse_templates(
                query="NDA for California",
                context=ctx,
            )
        assert result.success is True

    async def test_rag_failure_does_not_break_browse(
        self, clara_instance, mock_tool_executor, mock_retrieval_service_failure
    ):
        """Graceful degradation: RAG failure doesn't prevent template listing."""
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service_failure,
        ):
            result = await clara_instance.browse_templates(
                query="NDA templates",
                context=ctx,
            )
        # browse_templates should still work via PandaDoc API
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: generate_contract with RAG
# ---------------------------------------------------------------------------


class TestGenerateContractRAG:
    async def test_rag_injects_jurisdiction_context(
        self, clara_instance, mock_tool_executor, mock_retrieval_service
    ):
        from aspire_orchestrator.models import Outcome
        mock_tool_executor.return_value = _make_tool_result(
            tool_id="pandadoc.documents.create",
            data={"document_id": "doc-123", "status": "draft"},
        )
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service,
        ):
            result = await clara_instance.generate_contract(
                template_type="general_mutual_nda",
                parties=[{"name": "Acme Corp", "email": "acme@test.com"}],
                terms={"jurisdiction_state": "CA", "purpose": "Confidentiality", "term_length": "2 years"},
                context=ctx,
            )
        # RAG retrieval should have been called
        mock_retrieval_service.retrieve.assert_called_once()

    async def test_rag_failure_still_generates_contract(
        self, clara_instance, mock_tool_executor, mock_retrieval_service_failure
    ):
        """Graceful degradation: generates contract even without RAG."""
        mock_tool_executor.return_value = _make_tool_result(
            tool_id="pandadoc.documents.create",
            data={"document_id": "doc-456", "status": "draft"},
        )
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service_failure,
        ):
            result = await clara_instance.generate_contract(
                template_type="general_mutual_nda",
                parties=[{"name": "Acme Corp", "email": "acme@test.com"}],
                terms={"jurisdiction_state": "CA", "purpose": "Test", "term_length": "1 year"},
                context=ctx,
            )
        # Contract generation should complete even without RAG
        assert result is not None
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: sign_contract with RAG
# ---------------------------------------------------------------------------


class TestSignContractRAG:
    async def test_rag_adds_jurisdiction_requirements(
        self, clara_instance, mock_tool_executor, mock_retrieval_service
    ):
        mock_tool_executor.return_value = _make_tool_result(
            tool_id="pandadoc.documents.send",
            data={"status": "document.completed"},
        )
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service,
        ):
            result = await clara_instance.sign_contract(
                contract_id="doc-123",
                signer_info={"signer_name": "John Doe", "signer_email": "john@acme.com"},
                context=ctx,
            )
        # RAG should have been called for jurisdiction check
        mock_retrieval_service.retrieve.assert_called_once()

    async def test_rag_failure_does_not_block_signing(
        self, clara_instance, mock_tool_executor, mock_retrieval_service_failure
    ):
        """Graceful degradation: signing proceeds even if RAG fails."""
        mock_tool_executor.return_value = _make_tool_result(
            tool_id="pandadoc.documents.send",
            data={"status": "document.completed"},
        )
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service_failure,
        ):
            result = await clara_instance.sign_contract(
                contract_id="doc-123",
                signer_info={"signer_name": "John Doe", "signer_email": "john@acme.com"},
                context=ctx,
            )
        # Signing should still work even without RAG
        assert result is not None
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: review_contract_terms with RAG (EnhancedClaraLegal)
# ---------------------------------------------------------------------------


def _llm_success_return():
    """Return value matching call_llm's actual interface."""
    return {
        "content": "No critical issues found. Risk rating: LOW.",
        "model_used": "gpt-5-mini",
        "profile_used": "fallback",
        "receipt": None,
    }


class TestReviewContractTermsRAG:
    async def test_rag_enriches_review_prompt(
        self, enhanced_clara_instance, mock_retrieval_service
    ):
        """RAG should inject legal knowledge into the review prompt."""
        ctx = _make_agent_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service,
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal.get_template_spec",
            return_value={"lane": "general", "name": "NDA"},
        ), patch.object(
            enhanced_clara_instance, "call_llm",
            new_callable=AsyncMock,
            return_value=_llm_success_return(),
        ), patch.object(
            enhanced_clara_instance, "emit_receipt",
            new_callable=AsyncMock,
        ), patch.object(
            enhanced_clara_instance, "build_receipt",
            return_value={"receipt_id": "rcpt-test"},
        ):
            result = await enhanced_clara_instance.review_contract_terms(
                contract_text="This agreement contains standard terms...",
                contract_type="nda",
                ctx=ctx,
            )
        # RAG retrieval should have been called for legal standards
        mock_retrieval_service.retrieve.assert_called()

    async def test_rag_failure_still_reviews_terms(
        self, enhanced_clara_instance, mock_retrieval_service_failure
    ):
        """Graceful degradation: review works without RAG knowledge."""
        ctx = _make_agent_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=mock_retrieval_service_failure,
        ), patch(
            "aspire_orchestrator.skillpacks.clara_legal.get_template_spec",
            return_value={"lane": "general", "name": "NDA"},
        ), patch.object(
            enhanced_clara_instance, "call_llm",
            new_callable=AsyncMock,
            return_value=_llm_success_return(),
        ), patch.object(
            enhanced_clara_instance, "emit_receipt",
            new_callable=AsyncMock,
        ), patch.object(
            enhanced_clara_instance, "build_receipt",
            return_value={"receipt_id": "rcpt-test"},
        ):
            result = await enhanced_clara_instance.review_contract_terms(
                contract_text="This agreement contains standard terms...",
                contract_type="nda",
                ctx=ctx,
            )
        assert result is not None


# ---------------------------------------------------------------------------
# Tests: Graceful degradation patterns
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    async def test_import_error_does_not_crash(self, clara_instance, mock_tool_executor):
        """If legal_retrieval_service can't be imported, methods still work."""
        ctx = _make_clara_context()
        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            side_effect=ImportError("Module not found"),
        ):
            result = await clara_instance.browse_templates(
                query="NDA",
                context=ctx,
            )
        assert result.success is True

    async def test_timeout_in_rag_does_not_block(self, clara_instance, mock_tool_executor):
        """Timeout in RAG service doesn't block the main operation."""
        import asyncio
        ctx = _make_clara_context()

        svc = MagicMock()
        svc.retrieve = AsyncMock(side_effect=asyncio.TimeoutError("RAG timed out"))

        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=svc,
        ):
            result = await clara_instance.browse_templates(
                query="NDA",
                context=ctx,
            )
        assert result.success is True

    async def test_none_retrieval_result_handled(self, clara_instance, mock_tool_executor):
        """If retrieve() returns None, methods don't crash."""
        mock_tool_executor.return_value = _make_tool_result(
            tool_id="pandadoc.documents.create",
            data={"document_id": "doc-1", "status": "draft"},
        )
        ctx = _make_clara_context()

        svc = MagicMock()
        svc.retrieve = AsyncMock(return_value=None)
        svc.assemble_rag_context = MagicMock(return_value="")

        with patch(
            "aspire_orchestrator.services.legal_retrieval_service.get_retrieval_service",
            return_value=svc,
        ):
            result = await clara_instance.generate_contract(
                template_type="general_mutual_nda",
                parties=[{"name": "Acme", "email": "a@b.com"}],
                terms={"jurisdiction_state": "CA", "purpose": "Test", "term_length": "1 year"},
                context=ctx,
            )
        assert result is not None
        assert result.success is True
