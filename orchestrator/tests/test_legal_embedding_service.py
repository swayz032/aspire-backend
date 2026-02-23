"""Tests for legal_embedding_service.py — Clara RAG embedding layer.

All tests use mocked OpenAI client — no real API calls.
Covers: single embed, batch embed, rate limiting, API errors, receipts, validation.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class StubEmbeddingData:
    index: int
    embedding: list[float]


@dataclass
class StubUsage:
    total_tokens: int = 100


@dataclass
class StubEmbeddingResponse:
    data: list[StubEmbeddingData]
    usage: StubUsage = field(default_factory=StubUsage)


def _make_response(texts: list[str], dim: int = 3072) -> StubEmbeddingResponse:
    """Create a stub OpenAI embedding response."""
    return StubEmbeddingResponse(
        data=[
            StubEmbeddingData(index=i, embedding=[0.1] * dim)
            for i in range(len(texts))
        ],
        usage=StubUsage(total_tokens=len(texts) * 10),
    )


@pytest.fixture(autouse=True)
def _reset_module():
    """Reset module-level singletons between tests."""
    import aspire_orchestrator.services.legal_embedding_service as mod
    mod._client = None
    mod._rate_limiter = mod.EmbeddingRateLimiter(rate=10.0, burst=20)
    yield


@pytest.fixture
def mock_openai():
    """Mock the OpenAI client."""
    mock_client = AsyncMock()
    mock_client.embeddings = AsyncMock()
    with patch(
        "aspire_orchestrator.services.legal_embedding_service._get_client",
        return_value=mock_client,
    ):
        yield mock_client


@pytest.fixture
def mock_receipts():
    """Mock receipt store — must patch at source since embed_batch lazy-imports."""
    with patch(
        "aspire_orchestrator.services.receipt_store.store_receipts"
    ) as mock:
        yield mock


@pytest.fixture
def mock_settings():
    """Mock settings with embedding config."""
    with patch("aspire_orchestrator.services.legal_embedding_service.settings") as mock:
        mock.openai_api_key = "test-key"
        mock.openai_base_url = "https://api.openai.com/v1"
        mock.embedding_model = "text-embedding-3-large"
        mock.embedding_dimensions = 3072
        mock.embedding_batch_size = 50
        yield mock


# ---------------------------------------------------------------------------
# Tests: embed_text
# ---------------------------------------------------------------------------


class TestEmbedText:
    async def test_single_text_returns_vector(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_text

        mock_openai.embeddings.create = AsyncMock(
            return_value=_make_response(["test"])
        )

        result = await embed_text("test contract clause")
        assert len(result) == 3072
        assert all(isinstance(x, float) for x in result)

    async def test_empty_text_raises(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_text, EmbeddingError

        with pytest.raises(EmbeddingError, match="empty"):
            await embed_text("")

    async def test_whitespace_only_raises(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_text, EmbeddingError

        with pytest.raises(EmbeddingError, match="empty"):
            await embed_text("   \n\t  ")


# ---------------------------------------------------------------------------
# Tests: embed_batch
# ---------------------------------------------------------------------------


class TestEmbedBatch:
    async def test_batch_returns_correct_count(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_batch

        texts = ["clause one", "clause two", "clause three"]
        mock_openai.embeddings.create = AsyncMock(
            return_value=_make_response(texts)
        )

        result = await embed_batch(texts)
        assert len(result) == 3
        assert all(len(v) == 3072 for v in result)

    async def test_empty_list_raises(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_batch, EmbeddingError

        with pytest.raises(EmbeddingError, match="empty"):
            await embed_batch([])

    async def test_all_empty_strings_raises(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_batch, EmbeddingError

        with pytest.raises(EmbeddingError, match="empty"):
            await embed_batch(["", "  ", "\n"])

    async def test_receipt_emitted_on_success(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_batch

        mock_openai.embeddings.create = AsyncMock(
            return_value=_make_response(["test"])
        )

        await embed_batch(["test"])
        mock_receipts.assert_called_once()
        receipt = mock_receipts.call_args[0][0][0]
        assert receipt["outcome"] == "success"
        assert receipt["event_type"] == "rag.embed.success"

    async def test_receipt_emitted_on_api_error(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import embed_batch, EmbeddingError
        import openai

        mock_openai.embeddings.create = AsyncMock(
            side_effect=openai.APIError(
                message="rate limit",
                request=MagicMock(),
                body=None,
            )
        )

        with pytest.raises(EmbeddingError, match="API error"):
            await embed_batch(["test"])
        mock_receipts.assert_called_once()
        receipt = mock_receipts.call_args[0][0][0]
        assert receipt["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Tests: Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_rate_limit_exhaustion_raises(self, mock_openai, mock_receipts, mock_settings):
        from aspire_orchestrator.services.legal_embedding_service import (
            embed_batch,
            EmbeddingError,
            _rate_limiter,
        )
        import aspire_orchestrator.services.legal_embedding_service as mod

        # Exhaust all tokens
        mod._rate_limiter = mod.EmbeddingRateLimiter(rate=0.0, burst=0)

        with pytest.raises(EmbeddingError, match="rate limit"):
            await embed_batch(["test"])


# ---------------------------------------------------------------------------
# Tests: compute_content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic_hash(self):
        from aspire_orchestrator.services.legal_embedding_service import compute_content_hash

        h1 = compute_content_hash("test content")
        h2 = compute_content_hash("test content")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_different_content_different_hash(self):
        from aspire_orchestrator.services.legal_embedding_service import compute_content_hash

        h1 = compute_content_hash("content A")
        h2 = compute_content_hash("content B")
        assert h1 != h2
