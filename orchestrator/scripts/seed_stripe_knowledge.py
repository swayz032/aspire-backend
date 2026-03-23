#!/usr/bin/env python3
"""Seed Stripe API Knowledge Base — Real docs from stripe docs/.

Reads 58 .txt files of official Stripe API documentation, chunks them
intelligently, embeds via OpenAI text-embedding-3-large, and inserts into
finance_knowledge_chunks with domain='stripe_api'.

Usage:
    cd backend/orchestrator
    source ~/venvs/aspire/bin/activate
    python scripts/seed_stripe_knowledge.py

    # Or with explicit key:
    ASPIRE_OPENAI_API_KEY=sk-proj-... python scripts/seed_stripe_knowledge.py

Requires: ASPIRE_OPENAI_API_KEY or ASPIRE_OPENAI_KEY env var.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
import uuid
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Default stripe docs path — resolved relative to this script
# Script is at backend/orchestrator/scripts/, docs are at workspace root /stripe docs/
DEFAULT_STRIPE_DOCS_DIR = str(
    Path(__file__).resolve().parent.parent.parent.parent.parent / "stripe docs"
)

MAX_CHUNK_CHARS = 1500
MAX_EMBED_CHARS = 6000  # Hard cap — embedding model limit is 8192 tokens (~6K chars safe)
JSON_PREVIEW_LINES = 5

# Subdomain classification by filename keywords
_SUBDOMAIN_RULES: list[tuple[list[str], str]] = [
    (["invoice_item", "invoiceitem", "line item", "line_item", "bulk add", "bulk remove", "bulk update"], "invoice_line_items"),
    (["invoice payment", "invoicepayment"], "invoice_payments"),
    (["invoice", "finalize", "void", "pay invoice", "send invoice", "mark_invoice", "uncollectible", "detach_payment"], "invoices"),
    (["customer", "cus_"], "customers"),
    (["quote", "quotes"], "quotes"),
    (["payout"], "payouts"),
]


def classify_subdomain(filename: str) -> str:
    """Classify a stripe doc file into a subdomain."""
    lower = filename.lower()
    for keywords, subdomain in _SUBDOMAIN_RULES:
        if any(kw in lower for kw in keywords):
            return subdomain
    return "general"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def strip_json_blocks(text: str) -> str:
    """Replace large JSON code blocks with a short preview."""
    def _replace(match: re.Match) -> str:
        block = match.group(1)
        lines = block.strip().splitlines()
        if len(lines) <= JSON_PREVIEW_LINES:
            return match.group(0)  # Keep small blocks
        preview = "\n".join(lines[:JSON_PREVIEW_LINES])
        return f"```json\n{preview}\n  ... ({len(lines) - JSON_PREVIEW_LINES} more lines)\n```"

    return re.sub(r"```json\s*\n(.*?)```", _replace, text, flags=re.DOTALL)


def split_by_headers(text: str) -> list[str]:
    """Split text by ## headers, keeping header with its section."""
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, source_file: str) -> list[str]:
    """Chunk a stripe doc file intelligently.

    - Small (<2KB): single chunk
    - Medium (2-8KB): split by ## headers
    - Large (>8KB): split by ## headers, then sub-split long sections
    """
    # Strip verbose JSON response examples first
    text = strip_json_blocks(text)

    # For object definition files, extract field descriptions only
    lower_name = source_file.lower()
    is_object_file = "object" in lower_name

    char_count = len(text)

    if char_count < 2000:
        return [text]

    # Split by ## headers
    sections = split_by_headers(text)

    if not sections:
        return [text[:MAX_CHUNK_CHARS]]

    chunks: list[str] = []
    for section in sections:
        if len(section) <= MAX_CHUNK_CHARS:
            chunks.append(section)
        else:
            # Sub-split long sections by paragraphs
            _subsplit(section, chunks, is_object_file)

    return _enforce_max_embed(chunks)


def _subsplit(section: str, out: list[str], is_object_file: bool) -> None:
    """Sub-split a long section into ~MAX_CHUNK_CHARS pieces."""
    # For object files, split by top-level property definitions (- `field`)
    if is_object_file:
        parts = re.split(r"(?=^- `[a-z])", section, flags=re.MULTILINE)
    else:
        # Split by ### sub-headers first, then by double newlines
        parts = re.split(r"(?=^### )", section, flags=re.MULTILINE)
        if len(parts) <= 1:
            parts = section.split("\n\n")

    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(current) + len(part) + 2 > MAX_CHUNK_CHARS and current:
            out.append(current.strip())
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part

    if current.strip():
        out.append(current.strip())


def _enforce_max_embed(chunks: list[str]) -> list[str]:
    """Hard-cap: split any chunk exceeding MAX_EMBED_CHARS."""
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= MAX_EMBED_CHARS:
            result.append(chunk)
        else:
            # Force-split at paragraph boundaries within limit
            paragraphs = chunk.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) + 2 > MAX_EMBED_CHARS and current:
                    result.append(current.strip())
                    current = para
                else:
                    current = f"{current}\n\n{para}" if current else para
            if current.strip():
                # If single paragraph still too long, hard-truncate
                if len(current) > MAX_EMBED_CHARS:
                    result.append(current[:MAX_EMBED_CHARS].strip())
                else:
                    result.append(current.strip())
    return result


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def load_stripe_docs(docs_dir: str) -> list[dict]:
    """Load and chunk all .txt files from the stripe docs directory."""
    docs_path = Path(docs_dir)
    if not docs_path.is_dir():
        logger.error("Stripe docs directory not found: %s", docs_dir)
        sys.exit(1)

    txt_files = sorted(docs_path.glob("*.txt"))
    logger.info("Found %d .txt files in %s", len(txt_files), docs_dir)

    all_chunks: list[dict] = []

    for filepath in txt_files:
        filename = filepath.stem  # Without .txt
        subdomain = classify_subdomain(filename)

        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read %s: %s", filepath.name, e)
            continue

        chunks = chunk_text(content, filename)
        logger.info(
            "  %s → %d chunks (subdomain=%s, %d chars)",
            filepath.name, len(chunks), subdomain, len(content),
        )

        for chunk in chunks:
            all_chunks.append({
                "domain": "stripe_api",
                "subdomain": subdomain,
                "chunk_type": "api_reference",
                "content": chunk,
                "provider_name": "stripe",
                "source_id": filename,
            })

    logger.info("Total: %d chunks from %d files", len(all_chunks), len(txt_files))
    return all_chunks


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def compute_content_hash(content: str) -> str:
    """SHA-256 hash for dedup."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def resolve_openai_api_key() -> str:
    """Resolve OpenAI API key from env vars (multiple names)."""
    for var in ("ASPIRE_OPENAI_API_KEY", "ASPIRE_OPENAI_KEY", "OPENAI_API_KEY"):
        key = os.environ.get(var)
        if key:
            logger.info("Using OpenAI key from %s", var)
            return key
    logger.error(
        "No OpenAI API key found. Set ASPIRE_OPENAI_API_KEY or ASPIRE_OPENAI_KEY."
    )
    sys.exit(1)


async def seed_stripe_knowledge(docs_dir: str | None = None) -> None:
    """Embed and insert all Stripe doc chunks into finance_knowledge_chunks."""
    # Ensure key is available before loading heavy modules
    resolve_openai_api_key()

    from aspire_orchestrator.services.legal_embedding_service import (
        embed_batch,
    )
    from aspire_orchestrator.services.supabase_client import supabase_insert

    if docs_dir is None:
        docs_dir = os.environ.get("STRIPE_DOCS_DIR", DEFAULT_STRIPE_DOCS_DIR)

    chunks = load_stripe_docs(docs_dir)
    if not chunks:
        logger.error("No chunks to seed.")
        return

    total = len(chunks)
    batch_size = 10
    inserted = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c["content"] for c in batch]

        try:
            embeddings = await embed_batch(texts, suite_id="system")
        except Exception as e:
            logger.error("Embedding batch %d failed: %s", i // batch_size + 1, e)
            continue

        rows = []
        for j, chunk in enumerate(batch):
            content_hash = compute_content_hash(chunk["content"])
            row = {
                "id": str(uuid.uuid4()),
                "content": chunk["content"],
                "content_hash": content_hash,
                "embedding": f"[{','.join(str(x) for x in embeddings[j])}]",
                "domain": chunk["domain"],
                "subdomain": chunk.get("subdomain"),
                "chunk_type": chunk.get("chunk_type"),
                "provider_name": chunk.get("provider_name"),
                "is_active": True,
                "ingestion_receipt_id": f"seed-stripe-{uuid.uuid4().hex[:12]}",
            }
            rows.append(row)

        try:
            await supabase_insert("finance_knowledge_chunks", rows)
            inserted += len(rows)
            logger.info(
                "Batch %d/%d: inserted %d chunks (total: %d/%d)",
                i // batch_size + 1,
                (total + batch_size - 1) // batch_size,
                len(rows),
                inserted,
                total,
            )
        except Exception as e:
            err_msg = str(e)
            if "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
                skipped += len(rows)
                logger.info(
                    "Batch %d: %d chunks already exist (dedup)",
                    i // batch_size + 1,
                    len(rows),
                )
            else:
                logger.error("Insert batch %d failed: %s", i // batch_size + 1, e)

    logger.info(
        "Stripe seeding complete: %d inserted, %d skipped (dedup), %d total chunks",
        inserted,
        skipped,
        total,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed Stripe API docs into RAG")
    parser.add_argument(
        "--docs-dir",
        default=None,
        help="Path to stripe docs directory (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only chunk files, don't embed or insert",
    )
    args = parser.parse_args()

    if args.dry_run:
        docs_dir = args.docs_dir or os.environ.get(
            "STRIPE_DOCS_DIR", DEFAULT_STRIPE_DOCS_DIR,
        )
        chunks = load_stripe_docs(docs_dir)
        # Print subdomain distribution
        from collections import Counter

        dist = Counter(c["subdomain"] for c in chunks)
        print(f"\nChunk distribution ({len(chunks)} total):")
        for subdomain, count in dist.most_common():
            print(f"  {subdomain}: {count}")
    else:
        asyncio.run(seed_stripe_knowledge(args.docs_dir))
