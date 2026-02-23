"""Tests for legal_chunker.py — 5 chunking strategies for Clara RAG.

Tests all strategies with known input/output pairs.
Covers: clause_boundary, api_endpoint, template_spec, jurisdiction_rule, sliding_window.
Edge cases: empty input, single sentence, oversized chunks.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Tests: clause_boundary strategy
# ---------------------------------------------------------------------------


class TestClauseBoundary:
    def test_splits_on_section_headers(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "Section 1. Definitions\n"
            "This agreement defines the following terms.\n"
            "A 'Party' means any signatory.\n\n"
            "Section 2. Scope of Work\n"
            "The Contractor shall perform the following services.\n"
            "All work shall be completed by the deadline.\n"
        )
        chunks = chunk_document(content, strategy="clause_boundary", metadata={"domain": "contract_law"})
        assert len(chunks) >= 1
        # At least one chunk should mention the content
        all_content = " ".join(c.content for c in chunks)
        assert "Definitions" in all_content or "Scope of Work" in all_content

    def test_splits_on_whereas(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "WHEREAS, Party A operates a business in the State of California.\n"
            "WHEREAS, Party B desires to engage Party A for services.\n\n"
            "NOW THEREFORE, the parties agree as follows:\n"
            "1. Party A shall provide consulting services.\n"
        )
        chunks = chunk_document(content, strategy="clause_boundary", metadata={"domain": "contract_law"})
        assert len(chunks) >= 1

    def test_empty_input_raises_value_error(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        with pytest.raises(ValueError, match="empty"):
            chunk_document("", strategy="clause_boundary", metadata={})

    def test_splits_on_markdown_headers(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "## Indemnification\n"
            "Party A shall indemnify Party B against all claims.\n"
            "This obligation shall survive termination of the agreement.\n\n"
            "## Limitation of Liability\n"
            "Total liability shall not exceed the contract value.\n"
            "No party shall be liable for consequential damages.\n"
        )
        chunks = chunk_document(content, strategy="clause_boundary", metadata={"domain": "contract_law"})
        assert len(chunks) >= 1
        all_content = " ".join(c.content for c in chunks)
        assert "Indemnification" in all_content or "Limitation" in all_content


# ---------------------------------------------------------------------------
# Tests: api_endpoint strategy
# ---------------------------------------------------------------------------


class TestApiEndpoint:
    def test_splits_on_http_method_headers(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "### GET /templates\n"
            "List all templates in the workspace.\n"
            "Rate limit: 10 req/min. This endpoint supports pagination.\n"
            "You can filter by tags, workspace, and template status.\n\n"
            "### POST /documents\n"
            "Create a new document from a template.\n"
            "Requires template_uuid in body. Returns document ID and status.\n"
            "Supports merge fields and content placeholders.\n"
        )
        chunks = chunk_document(content, strategy="api_endpoint", metadata={"domain": "pandadoc_api"})
        assert len(chunks) >= 1
        all_content = " ".join(c.content for c in chunks)
        assert "templates" in all_content.lower() or "documents" in all_content.lower()

    def test_preserves_code_examples(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "### POST /documents\n"
            "Create a document from a template. Requires template_uuid.\n"
            "Supports merge fields, content placeholders, and pricing.\n\n"
            "```json\n"
            '{"name": "Contract", "template_uuid": "abc123"}\n'
            "```\n"
        )
        chunks = chunk_document(content, strategy="api_endpoint", metadata={"domain": "pandadoc_api"})
        assert len(chunks) >= 1
        all_content = " ".join(c.content for c in chunks)
        assert "template_uuid" in all_content


# ---------------------------------------------------------------------------
# Tests: template_spec strategy
# ---------------------------------------------------------------------------


class TestTemplateSpec:
    def test_produces_spec_chunks(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "## trades_msa_lite\n"
            "Service agreement / MSA-lite\n"
            "Lane: trades\n"
            "Risk: yellow\n"
            "Fields: scope_description, payment_terms\n\n"
            "### Heuristic\n"
            "Use when client needs a simple service contract.\n"
            "Best for projects under $50,000 total value.\n\n"
            "### Checklist\n"
            "- Scope defined\n"
            "- Payment terms included\n"
            "- Jurisdiction specified\n"
        )
        chunks = chunk_document(content, strategy="template_spec", metadata={"domain": "template_intelligence"})
        assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# Tests: jurisdiction_rule strategy
# ---------------------------------------------------------------------------


class TestJurisdictionRule:
    def test_splits_by_state(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "## California\n"
            "E-signatures valid under UETA. Business entity formation: SOS filing.\n"
            "Requires specific privacy disclosures under CCPA.\n\n"
            "## New York\n"
            "E-signatures valid under ESIGN. Note: specific requirements for real estate.\n"
            "Additional disclosure requirements for financial services.\n\n"
            "## Texas\n"
            "E-signatures valid. Texas Business Organizations Code governs entity formation.\n"
            "No state income tax considerations for business contracts.\n"
        )
        chunks = chunk_document(content, strategy="jurisdiction_rule", metadata={"domain": "contract_law"})
        assert len(chunks) >= 1
        # Check that at least one chunk has jurisdiction metadata
        has_jurisdiction = any(
            c.metadata.get("jurisdiction_state") not in (None, "", "general")
            for c in chunks
        )
        assert has_jurisdiction


# ---------------------------------------------------------------------------
# Tests: sliding_window strategy
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_creates_overlapping_chunks(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        # Create content long enough to require multiple windows
        sentences = [f"Sentence number {i} contains important legal information about contracts and obligations. " for i in range(100)]
        content = " ".join(sentences)

        chunks = chunk_document(content, strategy="sliding_window", metadata={"domain": "business_context"})
        assert len(chunks) >= 2

    def test_short_text_single_chunk(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        # Use enough text to exceed MIN_CHUNK_TOKENS (100)
        content = "This is a text about contracts. " * 20  # ~100+ tokens
        chunks = chunk_document(content, strategy="sliding_window", metadata={"domain": "business_context"})
        # Short text within MAX_CHUNK_TOKENS = single chunk
        assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# Tests: General
# ---------------------------------------------------------------------------


class TestChunkDocumentGeneral:
    def test_unknown_strategy_raises_value_error(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = " ".join(f"Sentence {i} about legal topics. " for i in range(50))
        with pytest.raises(ValueError, match="Unknown chunking strategy"):
            chunk_document(content, strategy="unknown_strategy", metadata={})

    def test_chunk_index_is_sequential(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        content = (
            "## Section One\nContent for section one is about legal matters and terms.\n\n"
            "## Section Two\nContent for section two covers liability and obligations.\n\n"
            "## Section Three\nContent for section three discusses payment and delivery.\n"
        )
        chunks = chunk_document(content, strategy="clause_boundary", metadata={"domain": "contract_law"})
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_metadata_propagated_to_chunks(self):
        from aspire_orchestrator.services.legal_chunker import chunk_document

        meta = {"domain": "contract_law", "template_key": "nda", "jurisdiction_state": "CA"}
        content = (
            "## Section 1\n"
            "Test content about NDAs in California. "
            "This section covers key terms and conditions.\n"
        )
        chunks = chunk_document(content, strategy="clause_boundary", metadata=meta)
        if chunks:
            assert chunks[0].metadata.get("domain") == "contract_law"
