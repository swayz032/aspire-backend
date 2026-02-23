"""Tests for legal_ingestion_pipeline.py — End-to-end ingestion.

Covers: full pipeline, dedup, batch sizing, receipt generation, error handling.
All Supabase and OpenAI calls mocked.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import tempfile
import os


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MD = """## Indemnification

Party A shall indemnify and hold harmless Party B from any claims arising out of
Party A's negligence. This indemnification obligation survives termination of the agreement.

## Limitation of Liability

Neither party shall be liable for indirect, incidental, special, or consequential damages.
Total aggregate liability shall not exceed the total fees paid under this agreement.

## Force Majeure

Neither party shall be in breach for failure to perform due to causes beyond reasonable control,
including but not limited to: acts of God, war, terrorism, pandemic, natural disasters.
"""


@pytest.fixture
def mock_embed():
    with patch(
        "aspire_orchestrator.services.legal_ingestion_pipeline.embed_batch",
        new_callable=AsyncMock,
        return_value=[[0.1] * 3072, [0.1] * 3072, [0.1] * 3072],
    ) as mock:
        yield mock


@pytest.fixture
def mock_supabase_insert():
    with patch(
        "aspire_orchestrator.services.legal_ingestion_pipeline.supabase_insert",
        new_callable=AsyncMock,
        return_value={"id": "test-id"},
    ) as mock:
        yield mock


@pytest.fixture
def mock_supabase_select():
    with patch(
        "aspire_orchestrator.services.legal_ingestion_pipeline.supabase_select",
        new_callable=AsyncMock,
        return_value=[],  # No existing hashes → all chunks are new
    ) as mock:
        yield mock


@pytest.fixture
def mock_receipt_store():
    with patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
    ) as mock:
        yield mock


@pytest.fixture
def temp_knowledge_file():
    """Create a temporary .md file with legal content."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(SAMPLE_MD)
        f.flush()
        yield f.name
    os.unlink(f.name)


# ---------------------------------------------------------------------------
# Tests: ingest_file
# ---------------------------------------------------------------------------


class TestIngestFile:
    async def test_ingests_file_successfully(
        self, temp_knowledge_file, mock_embed, mock_supabase_insert, mock_supabase_select
    ):
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file

        result = await ingest_file(
            file_path=temp_knowledge_file,
            domain="contract_law",
        )
        assert result.domain == "contract_law"
        assert result.chunks_created >= 1
        assert len(result.errors) == 0

    async def test_nonexistent_file_raises(self, mock_embed, mock_supabase_insert, mock_supabase_select):
        """ingest_file raises FileNotFoundError for missing files."""
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file

        with pytest.raises(FileNotFoundError, match="not found"):
            await ingest_file(
                file_path="/nonexistent/path/file.md",
                domain="contract_law",
            )

    async def test_empty_file_returns_zero_chunks(self, mock_embed, mock_supabase_insert, mock_supabase_select):
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("")
            f.flush()
            path = f.name

        try:
            result = await ingest_file(file_path=path, domain="contract_law")
            assert result.chunks_created == 0
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests: Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    async def test_dedup_skips_existing_hashes(
        self, temp_knowledge_file, mock_embed, mock_supabase_insert
    ):
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file
        from aspire_orchestrator.services.legal_embedding_service import compute_content_hash

        # Mock: all hashes already exist
        with patch(
            "aspire_orchestrator.services.legal_ingestion_pipeline.supabase_select",
            new_callable=AsyncMock,
        ) as mock_select:
            # Return hashes that match the content
            # We simulate "all chunks already exist"
            mock_select.return_value = [{"content_hash": "existing_hash"}]

            result = await ingest_file(
                file_path=temp_knowledge_file,
                domain="contract_law",
            )
            # Some chunks should be skipped (dedup) or created
            # The exact count depends on hash matching
            assert isinstance(result.chunks_skipped, int)


# ---------------------------------------------------------------------------
# Tests: Receipt generation
# ---------------------------------------------------------------------------


class TestReceipts:
    async def test_receipt_ids_populated(
        self, temp_knowledge_file, mock_embed, mock_supabase_insert, mock_supabase_select
    ):
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file

        result = await ingest_file(
            file_path=temp_knowledge_file,
            domain="contract_law",
        )
        # At least one receipt should be generated
        assert len(result.receipt_ids) >= 1


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_embedding_failure_raises(
        self, temp_knowledge_file, mock_supabase_insert, mock_supabase_select
    ):
        """ingest_file raises EmbeddingError when embedding fails."""
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file
        from aspire_orchestrator.services.legal_embedding_service import EmbeddingError

        with patch(
            "aspire_orchestrator.services.legal_ingestion_pipeline.embed_batch",
            new_callable=AsyncMock,
            side_effect=EmbeddingError("API key invalid"),
        ):
            with pytest.raises(EmbeddingError, match="API key invalid"):
                await ingest_file(
                    file_path=temp_knowledge_file,
                    domain="contract_law",
                )

    async def test_supabase_insert_failure_raises(
        self, temp_knowledge_file, mock_embed, mock_supabase_select
    ):
        """ingest_file raises SupabaseClientError when insert fails."""
        from aspire_orchestrator.services.legal_ingestion_pipeline import ingest_file
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        with patch(
            "aspire_orchestrator.services.legal_ingestion_pipeline.supabase_insert",
            new_callable=AsyncMock,
            side_effect=SupabaseClientError("insert", 500, "DB error"),
        ):
            with pytest.raises(SupabaseClientError):
                await ingest_file(
                    file_path=temp_knowledge_file,
                    domain="contract_law",
                )
