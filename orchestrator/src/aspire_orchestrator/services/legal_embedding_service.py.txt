"""Legal Embedding Service — Vector embeddings for Clara RAG knowledge base.

Uses OpenAI text-embedding-3-large (3072 dimensions) for high-quality
legal document embeddings. All calls produce receipts (Law #2).

Rate limiting: Token bucket (10 req/s burst 20) to stay within OpenAI limits.
Error handling: Fail-closed (Law #3) — raises on API errors.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import openai

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token Bucket Rate Limiter (reuses pattern from pandadoc_client.py)
# ---------------------------------------------------------------------------


class EmbeddingRateLimiter:
    """Token bucket rate limiter for OpenAI embedding API calls.

    Default: 10 req/s with burst 20 (conservative for text-embedding-3-large).
    """

    def __init__(self, rate: float = 10.0, burst: int = 20) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = max(0.0, min(self._burst, self._tokens + elapsed * self._rate))
        self._last_refill = now

    def acquire(self) -> bool:
        """Try to acquire a token. Returns True if allowed."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class EmbeddingError(Exception):
    """Raised when embedding operations fail (fail-closed, Law #3)."""

    def __init__(self, message: str, *, receipt_id: str = ""):
        self.receipt_id = receipt_id
        super().__init__(message)


# Module-level singleton
_rate_limiter = EmbeddingRateLimiter(rate=10.0, burst=20)
_client: openai.AsyncOpenAI | None = None


def _get_client() -> openai.AsyncOpenAI:
    """Get or create the async OpenAI client."""
    global _client
    if _client is None:
        api_key = settings.openai_api_key
        if not api_key:
            raise EmbeddingError(
                "OpenAI API key not configured (ASPIRE_OPENAI_API_KEY)",
                receipt_id="",
            )
        _client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=settings.openai_base_url,
            timeout=30.0,
        )
    return _client


def _build_receipt(
    *,
    event_type: str,
    outcome: str,
    reason_code: str,
    suite_id: str = "system",
    input_count: int = 0,
    output_dimensions: int = 0,
    model: str = "",
    tokens_used: int = 0,
    duration_ms: float = 0,
) -> dict[str, Any]:
    """Build a receipt dict for an embedding operation (Law #2)."""
    return {
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": "system",
        "suite_id": suite_id,
        "action_type": "rag.embed",
        "risk_tier": "green",
        "tool_used": "openai.embeddings",
        "outcome": outcome,
        "reason_code": reason_code,
        "input_count": input_count,
        "output_dimensions": output_dimensions,
        "model": model,
        "tokens_used": tokens_used,
        "duration_ms": round(duration_ms, 2),
    }


async def embed_text(
    text: str,
    *,
    suite_id: str = "system",
) -> list[float]:
    """Embed a single text string into a vector.

    Args:
        text: The text to embed.
        suite_id: Suite ID for receipt tracking.

    Returns:
        A list of floats (3072 dimensions by default).

    Raises:
        EmbeddingError: On API failure or rate limiting (fail-closed).
    """
    if not text or not text.strip():
        raise EmbeddingError("Cannot embed empty text", receipt_id="")

    result = await embed_batch([text], suite_id=suite_id)
    return result[0]


async def embed_batch(
    texts: list[str],
    *,
    suite_id: str = "system",
) -> list[list[float]]:
    """Embed a batch of texts into vectors.

    Splits into sub-batches of settings.embedding_batch_size if needed.

    Args:
        texts: List of texts to embed.
        suite_id: Suite ID for receipt tracking.

    Returns:
        List of embedding vectors (one per input text).

    Raises:
        EmbeddingError: On API failure or rate limiting (fail-closed).
    """
    from aspire_orchestrator.services.receipt_store import store_receipts

    if not texts:
        raise EmbeddingError("Cannot embed empty text list", receipt_id="")

    # Filter out empty strings
    clean_texts = [t for t in texts if t and t.strip()]
    if not clean_texts:
        raise EmbeddingError("All texts are empty after filtering", receipt_id="")

    model = settings.embedding_model
    dimensions = settings.embedding_dimensions
    batch_size = settings.embedding_batch_size

    all_embeddings: list[list[float]] = []

    # Process in sub-batches
    for i in range(0, len(clean_texts), batch_size):
        batch = clean_texts[i : i + batch_size]

        # Rate limiting
        if not _rate_limiter.acquire():
            receipt = _build_receipt(
                event_type="rag.embed.rate_limited",
                outcome="failed",
                reason_code="RATE_LIMITED",
                suite_id=suite_id,
                input_count=len(batch),
                model=model,
            )
            store_receipts([receipt])
            raise EmbeddingError(
                "Embedding rate limit exceeded (10 req/s)",
                receipt_id=receipt["receipt_id"],
            )

        start_ms = time.monotonic() * 1000
        try:
            client = _get_client()
            response = await client.embeddings.create(
                input=batch,
                model=model,
                dimensions=dimensions,
            )
        except openai.APIError as e:
            duration_ms = (time.monotonic() * 1000) - start_ms
            receipt = _build_receipt(
                event_type="rag.embed.failed",
                outcome="failed",
                reason_code="API_ERROR",
                suite_id=suite_id,
                input_count=len(batch),
                model=model,
                duration_ms=duration_ms,
            )
            store_receipts([receipt])
            raise EmbeddingError(
                f"OpenAI embedding API error: {e}",
                receipt_id=receipt["receipt_id"],
            ) from e
        except Exception as e:
            duration_ms = (time.monotonic() * 1000) - start_ms
            receipt = _build_receipt(
                event_type="rag.embed.failed",
                outcome="failed",
                reason_code="UNEXPECTED_ERROR",
                suite_id=suite_id,
                input_count=len(batch),
                model=model,
                duration_ms=duration_ms,
            )
            store_receipts([receipt])
            raise EmbeddingError(
                f"Embedding failed: {e}",
                receipt_id=receipt["receipt_id"],
            ) from e

        duration_ms = (time.monotonic() * 1000) - start_ms

        # Extract embeddings in input order
        sorted_data = sorted(response.data, key=lambda d: d.index)
        batch_embeddings = [d.embedding for d in sorted_data]
        all_embeddings.extend(batch_embeddings)

        # Compute token usage
        tokens_used = response.usage.total_tokens if response.usage else 0

        # Success receipt
        receipt = _build_receipt(
            event_type="rag.embed.success",
            outcome="success",
            reason_code="EXECUTED",
            suite_id=suite_id,
            input_count=len(batch),
            output_dimensions=dimensions,
            model=model,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
        )
        store_receipts([receipt])

        logger.info(
            "Embedded %d texts (%d tokens, %.0fms) — batch %d/%d",
            len(batch),
            tokens_used,
            duration_ms,
            (i // batch_size) + 1,
            (len(clean_texts) + batch_size - 1) // batch_size,
        )

    return all_embeddings


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of content for dedup purposes."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
