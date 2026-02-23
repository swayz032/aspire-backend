"""Legal Knowledge Ingestion Pipeline — Clara RAG (Law #2 compliant).

Pipeline: Source file → Read → Chunk (strategy auto-selected by domain) →
Dedup Check (content_hash via SHA-256) → Batch Embed → Upsert to Supabase →
Receipt → Source registry update.

All operations produce receipts. Fails closed on errors (Law #3).
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.services.legal_chunker import (
    ChunkResult,
    chunk_document,
)
from aspire_orchestrator.services.legal_embedding_service import (
    EmbeddingError,
    compute_content_hash,
    embed_batch,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)

logger = logging.getLogger(__name__)

# Batch size for Supabase inserts
_INSERT_BATCH_SIZE = 50

# Domain → default chunking strategy mapping
_DOMAIN_STRATEGY_MAP: dict[str, str] = {
    "contract_law": "clause_boundary",
    "api_reference": "api_endpoint",
    "template_guide": "template_spec",
    "jurisdiction": "jurisdiction_rule",
    "general": "sliding_window",
    "compliance": "clause_boundary",
    "tax": "jurisdiction_rule",
    "financial": "clause_boundary",
}


@dataclass
class IngestResult:
    """Result of ingesting a single file."""

    source_path: str
    domain: str
    chunks_created: int = 0
    chunks_skipped: int = 0  # dedup
    receipt_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _select_strategy(domain: str) -> str:
    """Auto-select chunking strategy based on domain."""
    return _DOMAIN_STRATEGY_MAP.get(domain, "sliding_window")


def _build_receipt(
    *,
    event_type: str,
    outcome: str,
    reason_code: str,
    suite_id: str = "system",
    source_path: str = "",
    domain: str = "",
    chunks_created: int = 0,
    chunks_skipped: int = 0,
    detail: str = "",
) -> dict[str, Any]:
    """Build a receipt for an ingestion operation (Law #2)."""
    return {
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": "system",
        "suite_id": suite_id,
        "action_type": "rag.ingest",
        "risk_tier": "green",
        "tool_used": "legal_ingestion_pipeline",
        "outcome": outcome,
        "reason_code": reason_code,
        "source_path": source_path,
        "domain": domain,
        "chunks_created": chunks_created,
        "chunks_skipped": chunks_skipped,
        "detail": detail,
    }


async def _check_existing_hashes(content_hashes: list[str]) -> set[str]:
    """Check which content hashes already exist in the knowledge base (dedup).

    Returns the set of hashes that already exist.
    """
    if not content_hashes:
        return set()

    existing: set[str] = set()
    # Query in batches to avoid URL length limits
    batch_size = 20
    for i in range(0, len(content_hashes), batch_size):
        batch = content_hashes[i : i + batch_size]
        hash_filter = ",".join(batch)
        try:
            rows = await supabase_select(
                "legal_knowledge_chunks",
                f"content_hash=in.({hash_filter})&select=content_hash",
            )
            for row in rows:
                h = row.get("content_hash", "")
                if h:
                    existing.add(h)
        except SupabaseClientError as e:
            # Dedup check failed — emit receipt (Law #2), then proceed without dedup
            from aspire_orchestrator.services.receipt_store import store_receipts
            receipt = _build_receipt(
                event_type="rag.ingest.dedup_check_failed",
                outcome="failed",
                reason_code="DEDUP_QUERY_ERROR",
                suite_id="system",
                detail=f"Batch {i}: {e}",
            )
            store_receipts([receipt])
            logger.warning("Dedup check failed — proceeding without dedup for batch %d", i)

    return existing


async def _batch_insert_chunks(
    records: list[dict[str, Any]],
    suite_id: str = "system",
) -> list[str]:
    """Insert chunk records into Supabase in batches of _INSERT_BATCH_SIZE.

    Returns list of receipt_ids for each batch insert.
    """
    from aspire_orchestrator.services.receipt_store import store_receipts

    receipt_ids: list[str] = []

    for i in range(0, len(records), _INSERT_BATCH_SIZE):
        batch = records[i : i + _INSERT_BATCH_SIZE]

        try:
            for record in batch:
                await supabase_insert("legal_knowledge_chunks", record)
        except SupabaseClientError as e:
            receipt = _build_receipt(
                event_type="rag.ingest.insert_failed",
                outcome="failed",
                reason_code="INSERT_FAILED",
                suite_id=suite_id,
                detail=f"Batch {i // _INSERT_BATCH_SIZE}: {e}",
            )
            store_receipts([receipt])
            receipt_ids.append(receipt["receipt_id"])
            raise

        receipt = _build_receipt(
            event_type="rag.ingest.batch_inserted",
            outcome="success",
            reason_code="EXECUTED",
            suite_id=suite_id,
            chunks_created=len(batch),
            detail=f"Batch {i // _INSERT_BATCH_SIZE + 1}/{(len(records) + _INSERT_BATCH_SIZE - 1) // _INSERT_BATCH_SIZE}",
        )
        store_receipts([receipt])
        receipt_ids.append(receipt["receipt_id"])

        logger.info(
            "Inserted batch %d/%d (%d chunks)",
            i // _INSERT_BATCH_SIZE + 1,
            (len(records) + _INSERT_BATCH_SIZE - 1) // _INSERT_BATCH_SIZE,
            len(batch),
        )

    return receipt_ids


async def ingest_file(
    file_path: str,
    domain: str,
    source_type: str = "file",
    suite_id: str | None = None,
) -> IngestResult:
    """Ingest a single file into the Clara RAG knowledge base.

    Pipeline: Read → Chunk → Dedup → Embed → Insert → Receipt.

    Args:
        file_path: Path to the source file.
        domain: Knowledge domain (e.g., "contract_law", "api_reference").
        source_type: Source type identifier (default "file").
        suite_id: Optional suite ID for tenant-scoped knowledge.

    Returns:
        IngestResult with counts and receipt IDs.

    Raises:
        FileNotFoundError: If file does not exist.
        EmbeddingError: On embedding API failure.
        SupabaseClientError: On database insert failure.
    """
    from aspire_orchestrator.services.receipt_store import store_receipts

    resolved_suite_id = suite_id or "system"
    result = IngestResult(source_path=file_path, domain=domain)

    # 1. Read file
    path = Path(file_path)
    if not path.exists():
        receipt = _build_receipt(
            event_type="rag.ingest.file_not_found",
            outcome="failed",
            reason_code="FILE_NOT_FOUND",
            suite_id=resolved_suite_id,
            source_path=file_path,
            domain=domain,
        )
        store_receipts([receipt])
        result.receipt_ids.append(receipt["receipt_id"])
        raise FileNotFoundError(f"Source file not found: {file_path}")

    content = path.read_text(encoding="utf-8")
    if not content.strip():
        receipt = _build_receipt(
            event_type="rag.ingest.empty_file",
            outcome="failed",
            reason_code="EMPTY_FILE",
            suite_id=resolved_suite_id,
            source_path=file_path,
            domain=domain,
        )
        store_receipts([receipt])
        result.receipt_ids.append(receipt["receipt_id"])
        result.errors.append(f"Empty file: {file_path}")
        return result

    # 2. Chunk
    strategy = _select_strategy(domain)
    base_metadata = {
        "domain": domain,
        "source_type": source_type,
        "source_path": file_path,
        "source_filename": path.name,
    }

    chunks = chunk_document(content, strategy, metadata=base_metadata)
    if not chunks:
        receipt = _build_receipt(
            event_type="rag.ingest.no_chunks",
            outcome="failed",
            reason_code="NO_CHUNKS_PRODUCED",
            suite_id=resolved_suite_id,
            source_path=file_path,
            domain=domain,
        )
        store_receipts([receipt])
        result.receipt_ids.append(receipt["receipt_id"])
        result.errors.append("Chunker produced no chunks")
        return result

    # 3. Dedup — compute content hashes and check for existing
    chunk_hashes = [compute_content_hash(c.content) for c in chunks]
    existing_hashes = await _check_existing_hashes(chunk_hashes)

    new_chunks: list[ChunkResult] = []
    new_hashes: list[str] = []
    for chunk, h in zip(chunks, chunk_hashes):
        if h in existing_hashes:
            result.chunks_skipped += 1
        else:
            new_chunks.append(chunk)
            new_hashes.append(h)

    if not new_chunks:
        receipt = _build_receipt(
            event_type="rag.ingest.all_deduped",
            outcome="success",
            reason_code="ALL_CHUNKS_EXIST",
            suite_id=resolved_suite_id,
            source_path=file_path,
            domain=domain,
            chunks_skipped=result.chunks_skipped,
        )
        store_receipts([receipt])
        result.receipt_ids.append(receipt["receipt_id"])
        logger.info(
            "All %d chunks already exist — skipping %s",
            result.chunks_skipped, file_path,
        )
        return result

    # 4. Embed
    texts = [c.content for c in new_chunks]
    embeddings = await embed_batch(texts, suite_id=resolved_suite_id)

    # 5. Build records for insert
    source_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []

    for chunk, embedding, content_hash in zip(new_chunks, embeddings, new_hashes):
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "source_id": source_id,
            "domain": domain,
            "chunk_type": chunk.chunk_type,
            "chunk_index": chunk.chunk_index,
            "parent_chunk_id": None,  # parent_index is int; DB expects UUID — resolve in post-processing
            "content": chunk.content,
            "content_hash": content_hash,
            "token_count": chunk.token_count,
            "embedding": f"[{','.join(str(x) for x in embedding)}]",  # pgvector string format
            "metadata": chunk.metadata,
            "created_at": now_iso,
        }
        if suite_id:
            record["suite_id"] = suite_id
        records.append(record)

    # 6. Batch insert
    insert_receipt_ids = await _batch_insert_chunks(records, suite_id=resolved_suite_id)
    result.receipt_ids.extend(insert_receipt_ids)
    result.chunks_created = len(records)

    # 7. Update source registry
    try:
        await supabase_insert("legal_knowledge_sources", {
            "id": source_id,
            "source_path": file_path,
            "source_type": source_type,
            "domain": domain,
            "chunk_count": len(records),
            "chunk_strategy": strategy,
            "file_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "ingested_at": now_iso,
        })
    except SupabaseClientError as e:
        logger.warning("Source registry update failed (non-fatal): %s", e)

    # 8. Final receipt
    receipt = _build_receipt(
        event_type="rag.ingest.complete",
        outcome="success",
        reason_code="EXECUTED",
        suite_id=resolved_suite_id,
        source_path=file_path,
        domain=domain,
        chunks_created=result.chunks_created,
        chunks_skipped=result.chunks_skipped,
    )
    store_receipts([receipt])
    result.receipt_ids.append(receipt["receipt_id"])

    logger.info(
        "Ingested %s: domain=%s, created=%d, skipped=%d, strategy=%s",
        file_path, domain, result.chunks_created, result.chunks_skipped, strategy,
    )

    return result


async def ingest_directory(
    dir_path: str,
    domain: str,
    *,
    source_type: str = "file",
    suite_id: str | None = None,
    extensions: tuple[str, ...] = (".md", ".txt", ".yaml", ".json"),
) -> list[IngestResult]:
    """Ingest all matching files from a directory.

    Args:
        dir_path: Directory path to scan.
        domain: Knowledge domain for all files.
        source_type: Source type identifier.
        suite_id: Optional suite ID.
        extensions: File extensions to include.

    Returns:
        List of IngestResult, one per file processed.
    """
    from aspire_orchestrator.services.receipt_store import store_receipts

    resolved_suite_id = suite_id or "system"
    directory = Path(dir_path)

    if not directory.is_dir():
        receipt = _build_receipt(
            event_type="rag.ingest.dir_not_found",
            outcome="failed",
            reason_code="DIRECTORY_NOT_FOUND",
            suite_id=resolved_suite_id,
            source_path=dir_path,
            domain=domain,
        )
        store_receipts([receipt])
        raise FileNotFoundError(f"Directory not found: {dir_path}")

    files = sorted(
        f for f in directory.rglob("*")
        if f.is_file() and f.suffix.lower() in extensions
    )

    if not files:
        logger.info("No matching files in %s (extensions: %s)", dir_path, extensions)
        return []

    results: list[IngestResult] = []
    for file_path in files:
        try:
            result = await ingest_file(
                str(file_path),
                domain=domain,
                source_type=source_type,
                suite_id=suite_id,
            )
            results.append(result)
        except Exception as e:
            logger.error("Failed to ingest %s: %s", file_path, e)
            error_result = IngestResult(
                source_path=str(file_path),
                domain=domain,
                errors=[str(e)],
            )
            results.append(error_result)

    total_created = sum(r.chunks_created for r in results)
    total_skipped = sum(r.chunks_skipped for r in results)
    logger.info(
        "Directory ingestion complete: %s — files=%d, chunks_created=%d, chunks_skipped=%d",
        dir_path, len(results), total_created, total_skipped,
    )

    return results
