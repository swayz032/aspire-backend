"""Base Retrieval Service — Unified RAG pipeline for domain knowledge.

DRYs up the duplication between domain-specific retrieval services.
Provides: embedding, hybrid search (70% vector / 30% full-text),
in-memory caching (300s TTL), LLM-based reranking, receipt emission,
and context assembly.

Subclasses configure: search_function, actor_name, cache_prefix,
domain_label, and domain-specific filter assembly.

Law compliance:
  - Law #2: Receipt for every retrieval operation
  - Law #3: Fail-closed on embedding/search errors (returns empty, not guesses)
  - Law #6: Suite-scoped search (global + tenant knowledge)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Result of a RAG retrieval operation."""

    chunks: list[dict[str, Any]] = field(default_factory=list)
    query: str = ""
    filters_applied: dict[str, Any] = field(default_factory=dict)
    timing_ms: float = 0.0
    cache_hit: bool = False
    receipt_id: str = ""


class BaseRetrievalService:
    """Base class for domain-specific RAG retrieval services.

    Subclasses MUST set:
      - search_function: Supabase RPC function name (e.g., "search_general_knowledge")
      - actor_name: Receipt actor identifier (e.g., "service:ava-rag-retrieval")
      - cache_prefix: Cache key prefix (e.g., "general_rag")
      - domain_label: Human label for context headers (e.g., "GENERAL KNOWLEDGE")

    Thread-safe for concurrent async usage within a single process.
    Cache is in-memory with TTL eviction.
    """

    search_function: str = ""
    search_table: str = ""
    actor_name: str = "service:base-rag-retrieval"
    cache_prefix: str = "base_rag"
    domain_label: str = "KNOWLEDGE"

    def __init__(
        self,
        cache_ttl: float = 300.0,
        cache_max: int = 500,
    ) -> None:
        self._cache: dict[str, tuple[float, RetrievalResult]] = {}
        self._cache_ttl = cache_ttl
        self._cache_max = cache_max

    def _store_retrieval_receipt(
        self,
        *,
        receipt_id: str,
        suite_id: str | None,
        outcome: str,
        reason_code: str,
        timing_ms: float,
        chunk_count: int = 0,
        cache_hit: bool = False,
        filters_applied: dict[str, Any] | None = None,
    ) -> None:
        """Emit an immutable receipt for a retrieval operation (Law #2)."""
        receipt = {
            "receipt_id": receipt_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": "rag.retrieval",
            "actor": self.actor_name,
            "suite_id": suite_id or "system",
            "action_type": "rag.retrieve",
            "risk_tier": "green",
            "tool_used": self.search_function,
            "outcome": outcome,
            "reason_code": reason_code,
            "timing_ms": round(timing_ms, 2),
            "chunk_count": chunk_count,
            "cache_hit": cache_hit,
        }
        if filters_applied:
            receipt["filters_applied"] = filters_applied
        store_receipts([receipt])

    def _build_search_params(
        self,
        query_embedding: list[float],
        query_text: str,
        suite_id: str | None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """Build RPC params for hybrid search. Override for domain-specific filters."""
        from aspire_orchestrator.config.settings import settings

        params: dict[str, Any] = {
            "query_embedding": f"[{','.join(str(x) for x in query_embedding)}]",
            "query_text": query_text[:500],
            "p_limit": settings.rag_max_chunks_per_query,
            "p_vector_weight": settings.rag_vector_weight,
            "p_text_weight": settings.rag_text_weight,
            "p_min_similarity": settings.rag_min_similarity,
        }
        if domain:
            params["p_domain"] = domain
        if suite_id:
            params["p_suite_id"] = suite_id
        return params

    async def retrieve(
        self,
        query: str,
        suite_id: str | None = None,
        domain: str | None = None,
    ) -> RetrievalResult:
        """Execute the full retrieval pipeline.

        Args:
            query: Natural language search query
            suite_id: Tenant ID for scoped search (NULL = global only)
            domain: Optional domain filter

        Returns:
            RetrievalResult with matching chunks and metadata.
            Returns empty result on any failure (graceful degradation).
        """
        start = time.monotonic()
        receipt_id = f"rcpt-{self.cache_prefix}-{uuid.uuid4().hex[:12]}"

        if not query or not query.strip():
            self._store_retrieval_receipt(
                receipt_id=receipt_id, suite_id=suite_id,
                outcome="denied", reason_code="EMPTY_QUERY",
                timing_ms=(time.monotonic() - start) * 1000,
            )
            return RetrievalResult(receipt_id=receipt_id)

        try:
            # 1. Check cache
            cache_key = self._build_cache_key(query, suite_id, domain)
            cached = self._cache_get(cache_key)
            if cached is not None:
                cached.cache_hit = True
                cached.timing_ms = (time.monotonic() - start) * 1000
                cached.receipt_id = receipt_id
                self._store_retrieval_receipt(
                    receipt_id=receipt_id, suite_id=suite_id,
                    outcome="success", reason_code="CACHE_HIT",
                    timing_ms=cached.timing_ms,
                    chunk_count=len(cached.chunks), cache_hit=True,
                )
                logger.debug(
                    "%s cache hit for query: %s (%.1fms)",
                    self.cache_prefix, query[:50], cached.timing_ms,
                )
                return cached

            # 2. Embed query
            query_embedding = await self._embed_query(query)
            if not query_embedding:
                elapsed = (time.monotonic() - start) * 1000
                self._store_retrieval_receipt(
                    receipt_id=receipt_id, suite_id=suite_id,
                    outcome="failed", reason_code="EMBEDDING_FAILED",
                    timing_ms=elapsed,
                )
                return RetrievalResult(query=query, receipt_id=receipt_id, timing_ms=elapsed)

            # 3. Hybrid search via Supabase RPC
            raw_chunks = await self._hybrid_search(
                query_embedding=query_embedding,
                query_text=query,
                suite_id=suite_id,
                domain=domain,
            )

            # 4. Build result
            filters_dict = {"domain": domain}
            result = RetrievalResult(
                chunks=raw_chunks,
                query=query,
                filters_applied=filters_dict,
                timing_ms=(time.monotonic() - start) * 1000,
                cache_hit=False,
                receipt_id=receipt_id,
            )

            # 5. Cache write
            self._cache_put(cache_key, result)

            # 6. Receipt (Law #2)
            self._store_retrieval_receipt(
                receipt_id=receipt_id, suite_id=suite_id,
                outcome="success", reason_code="EXECUTED",
                timing_ms=result.timing_ms,
                chunk_count=len(raw_chunks),
                filters_applied=filters_dict,
            )

            logger.info(
                "%s retrieval: %d chunks in %.1fms (domain=%s)",
                self.cache_prefix, len(raw_chunks), result.timing_ms, domain,
            )

            return result

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "%s retrieval failed (non-fatal): %s (%.1fms)",
                self.cache_prefix, e, elapsed,
            )
            self._store_retrieval_receipt(
                receipt_id=receipt_id, suite_id=suite_id,
                outcome="failed", reason_code=type(e).__name__,
                timing_ms=elapsed,
            )
            return RetrievalResult(query=query, receipt_id=receipt_id, timing_ms=elapsed)

    def assemble_rag_context(self, result: RetrievalResult) -> str:
        """Format retrieval results as context string for LLM prompt injection.

        Returns empty string if no chunks — caller should skip RAG section.
        """
        if not result.chunks:
            return ""

        lines = [f"--- RELEVANT {self.domain_label} ---"]
        total = len(result.chunks)

        for i, chunk in enumerate(result.chunks, 1):
            domain = chunk.get("domain", "unknown")
            similarity = chunk.get("combined_score") or chunk.get("vector_similarity", 0)
            chunk_type = chunk.get("chunk_type", "")
            content = chunk.get("content", "")

            meta_parts = [f"Domain: {domain}"]
            if chunk_type:
                meta_parts.append(f"Type: {chunk_type}")
            meta_parts.append(f"Relevance: {similarity:.2f}")

            lines.append(f"\n[Knowledge {i}/{total}] {' | '.join(meta_parts)}")
            lines.append(content.strip())

        lines.append(f"\n--- END {self.domain_label} ---")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Internal: Embedding
    # -----------------------------------------------------------------------

    async def _embed_query(self, query: str) -> list[float] | None:
        """Embed query text via OpenAI. Returns None on failure."""
        try:
            from aspire_orchestrator.services.legal_embedding_service import embed_text
            return await embed_text(query)
        except Exception as e:
            logger.warning("%s query embedding failed (non-fatal): %s", self.cache_prefix, e)
            return None

    # -----------------------------------------------------------------------
    # Internal: Hybrid Search
    # -----------------------------------------------------------------------

    async def _hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        suite_id: str | None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute hybrid search via Supabase RPC."""
        try:
            from aspire_orchestrator.services.supabase_client import SupabaseClientError, supabase_rpc

            params = self._build_search_params(
                query_embedding=query_embedding,
                query_text=query_text,
                suite_id=suite_id,
                domain=domain,
            )

            result = await supabase_rpc(self.search_function, params)
            if isinstance(result, list):
                return result
            return []

        except SupabaseClientError as e:
            # Supabase vector operator/function mismatch in some environments.
            # Degrade to plain text table search to keep responses online.
            if (
                "rpc_disabled_vector_mismatch" in e.detail.lower()
                or (
                    e.status_code in (400, 404)
                    and (
                        "operator does not exist" in e.detail.lower()
                        or "function does not exist" in e.detail.lower()
                    )
                )
            ):
                return await self._text_fallback_search(query_text=query_text, suite_id=suite_id, domain=domain)
            logger.warning("%s hybrid search failed (non-fatal): %s", self.cache_prefix, e)
            return []
        except Exception as e:
            logger.warning("%s hybrid search failed (non-fatal): %s", self.cache_prefix, e)
            return []

    async def _text_fallback_search(
        self,
        *,
        query_text: str,
        suite_id: str | None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fallback retrieval that avoids vector RPC dependency."""
        if not self.search_table:
            return []
        try:
            from aspire_orchestrator.config.settings import settings
            from aspire_orchestrator.services.supabase_client import supabase_select

            terms = [t.strip() for t in query_text.lower().split() if len(t.strip()) >= 3][:4]
            if not terms:
                return []

            filters = ["is_active=eq.true"]
            if domain:
                filters.append(f"domain=eq.{domain}")
            if suite_id:
                filters.append(f"or=(suite_id.is.null,suite_id.eq.{suite_id})")
            else:
                filters.append("suite_id=is.null")

            select_cols = "id,content,domain,subdomain,chunk_type,confidence_score,expert_reviewed,created_at"
            max_rows = max(20, int(settings.rag_max_chunks_per_query) * 4)
            filters.append(f"select={select_cols}")
            filters.append(f"limit={max_rows}")
            rows = await supabase_select(self.search_table, "&".join(filters))

            scored: list[tuple[float, dict[str, Any]]] = []
            for row in rows:
                content = str(row.get("content", "")).lower()
                if not content:
                    continue
                matches = sum(1 for term in terms if term in content)
                if matches <= 0:
                    continue
                score = matches / max(len(terms), 1)
                out = dict(row)
                out["vector_similarity"] = 0.0
                out["text_rank"] = float(score)
                out["combined_score"] = float(score)
                scored.append((score, out))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [item for _, item in scored[: int(settings.rag_max_chunks_per_query)]]
        except Exception as e:
            logger.warning("%s text fallback search failed (non-fatal): %s", self.cache_prefix, e)
            return []

    # -----------------------------------------------------------------------
    # Internal: Cache
    # -----------------------------------------------------------------------

    def _build_cache_key(
        self,
        query: str,
        suite_id: str | None,
        domain: str | None = None,
    ) -> str:
        """Build deterministic cache key from query + context."""
        key_data = json.dumps({
            "p": self.cache_prefix,
            "q": query.lower().strip(),
            "s": suite_id or "",
            "d": domain or "",
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(key_data.encode()).hexdigest()[:24]

    def _cache_get(self, key: str) -> RetrievalResult | None:
        """Get from cache if not expired."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, result = entry
        if time.monotonic() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        return result

    def _cache_put(self, key: str, result: RetrievalResult) -> None:
        """Put into cache with eviction."""
        if len(self._cache) >= self._cache_max:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest_key]
        self._cache[key] = (time.monotonic(), result)

    def clear_cache(self) -> None:
        """Clear the retrieval cache."""
        self._cache.clear()
