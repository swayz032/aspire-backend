"""Legal Document Chunker — 5 strategies for Clara RAG knowledge base.

Strategies:
  1. clause_boundary: Legal clauses (Section, Article, WHEREAS, etc.)
  2. api_endpoint: API docs split by endpoint (HTTP methods, headers)
  3. template_spec: 3 chunks per template from registry JSON
  4. jurisdiction_rule: One chunk per state per topic
  5. sliding_window: Token-based windows with overlap (fallback)

Chunk size: min 100 tokens, target 300-500, max 800.
Uses tiktoken for accurate token counting.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

import tiktoken

logger = logging.getLogger(__name__)

# Tiktoken encoder for cl100k_base (used by text-embedding-3-large)
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """Get or create tiktoken encoder (cached)."""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken."""
    return len(_get_encoder().encode(text))


# ---------------------------------------------------------------------------
# Chunk size constraints
# ---------------------------------------------------------------------------

MIN_CHUNK_TOKENS = 100
TARGET_CHUNK_TOKENS = 400
MAX_CHUNK_TOKENS = 800


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ChunkResult:
    """A single chunk produced by the chunker."""

    content: str
    chunk_type: str
    chunk_index: int
    parent_index: int | None = None
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strategy: clause_boundary
# ---------------------------------------------------------------------------

# Patterns that mark legal clause boundaries
_CLAUSE_PATTERNS = re.compile(
    r"^(?:"
    r"(?:Section|SECTION)\s+\d+"
    r"|(?:Article|ARTICLE)\s+\d+"
    r"|WHEREAS"
    r"|NOW\s+THEREFORE"
    r"|IN\s+WITNESS\s+WHEREOF"
    r"|##\s+"
    r")",
    re.MULTILINE,
)


def _chunk_clause_boundary(content: str, metadata: dict[str, Any]) -> list[ChunkResult]:
    """Split on legal clause boundaries."""
    splits = _CLAUSE_PATTERNS.split(content)
    # Get the actual headers to use as context
    headers = _CLAUSE_PATTERNS.findall(content)

    chunks: list[ChunkResult] = []
    for i, segment in enumerate(splits):
        text = segment.strip()
        if not text:
            continue
        # Prepend the header that preceded this segment
        if i > 0 and i - 1 < len(headers):
            text = headers[i - 1].strip() + "\n" + text

        chunks.append(ChunkResult(
            content=text,
            chunk_type="clause_boundary",
            chunk_index=len(chunks),
            metadata={**metadata, "section_header": headers[i - 1].strip() if i > 0 and i - 1 < len(headers) else ""},
        ))

    return _enforce_size_limits(chunks)


# ---------------------------------------------------------------------------
# Strategy: api_endpoint
# ---------------------------------------------------------------------------

_API_ENDPOINT_PATTERN = re.compile(
    r"^(?:###\s+|(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)",
    re.MULTILINE,
)


def _chunk_api_endpoint(content: str, metadata: dict[str, Any]) -> list[ChunkResult]:
    """Split API documentation by endpoint (### headers or HTTP methods)."""
    # Find all match positions
    matches = list(_API_ENDPOINT_PATTERN.finditer(content))
    if not matches:
        # No endpoints found — treat as single chunk
        return _chunk_sliding_window(content, metadata)

    chunks: list[ChunkResult] = []

    # Content before first endpoint
    preamble = content[:matches[0].start()].strip()
    if preamble and _count_tokens(preamble) >= MIN_CHUNK_TOKENS:
        chunks.append(ChunkResult(
            content=preamble,
            chunk_type="api_preamble",
            chunk_index=0,
            metadata={**metadata, "endpoint": "preamble"},
        ))

    parent_idx = len(chunks)
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        segment = content[match.start():end].strip()
        if not segment:
            continue

        # Extract endpoint name from first line
        first_line = segment.split("\n", 1)[0].strip()

        # Check for code blocks — make them child chunks
        code_blocks = list(re.finditer(r"```[\s\S]*?```", segment))
        if code_blocks and _count_tokens(segment) > MAX_CHUNK_TOKENS:
            # Split: prose part + code examples as children
            prose_end = code_blocks[0].start()
            prose = segment[:prose_end].strip()
            if prose:
                main_idx = len(chunks)
                chunks.append(ChunkResult(
                    content=prose,
                    chunk_type="api_endpoint",
                    chunk_index=main_idx,
                    metadata={**metadata, "endpoint": first_line},
                ))
                for cb in code_blocks:
                    code = cb.group().strip()
                    if code and _count_tokens(code) >= MIN_CHUNK_TOKENS:
                        chunks.append(ChunkResult(
                            content=code,
                            chunk_type="api_code_example",
                            chunk_index=len(chunks),
                            parent_index=main_idx,
                            metadata={**metadata, "endpoint": first_line},
                        ))
        else:
            chunks.append(ChunkResult(
                content=segment,
                chunk_type="api_endpoint",
                chunk_index=len(chunks),
                metadata={**metadata, "endpoint": first_line},
            ))

    return _enforce_size_limits(chunks)


# ---------------------------------------------------------------------------
# Strategy: template_spec
# ---------------------------------------------------------------------------


def _chunk_template_spec(content: str, metadata: dict[str, Any]) -> list[ChunkResult]:
    """Produce 3 chunks per template: spec, heuristic, checklist.

    Expects structured content with sections like:
    - Specification / Overview
    - Heuristics / Rules / Guidelines
    - Checklist / Validation / Requirements
    """
    chunks: list[ChunkResult] = []
    template_key = metadata.get("template_key", "unknown")

    # Try to split by ## headers
    sections = re.split(r"^##\s+", content, flags=re.MULTILINE)

    spec_text = ""
    heuristic_text = ""
    checklist_text = ""

    for section in sections:
        if not section.strip():
            continue
        lower = section.lower()
        first_line = section.split("\n", 1)[0].strip().lower()

        if any(kw in first_line for kw in ("spec", "overview", "description", "purpose")):
            spec_text += section.strip() + "\n\n"
        elif any(kw in first_line for kw in ("heuristic", "rule", "guideline", "usage")):
            heuristic_text += section.strip() + "\n\n"
        elif any(kw in first_line for kw in ("checklist", "validation", "requirement", "field")):
            checklist_text += section.strip() + "\n\n"
        else:
            # Default: add to spec
            spec_text += section.strip() + "\n\n"

    # If we couldn't split meaningfully, use thirds
    if not heuristic_text and not checklist_text:
        lines = content.strip().split("\n")
        third = max(1, len(lines) // 3)
        spec_text = "\n".join(lines[:third])
        heuristic_text = "\n".join(lines[third : 2 * third])
        checklist_text = "\n".join(lines[2 * third :])

    for chunk_type, text in [
        ("template_spec", spec_text),
        ("template_heuristic", heuristic_text),
        ("template_checklist", checklist_text),
    ]:
        text = text.strip()
        if text:
            chunks.append(ChunkResult(
                content=text,
                chunk_type=chunk_type,
                chunk_index=len(chunks),
                metadata={**metadata, "template_key": template_key},
            ))

    return _enforce_size_limits(chunks)


# ---------------------------------------------------------------------------
# Strategy: jurisdiction_rule
# ---------------------------------------------------------------------------

_STATE_HEADER_PATTERN = re.compile(
    r"^(?:##\s+)?(?:State:\s*)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*$",
    re.MULTILINE,
)


def _chunk_jurisdiction_rule(content: str, metadata: dict[str, Any]) -> list[ChunkResult]:
    """One chunk per state per topic."""
    matches = list(_STATE_HEADER_PATTERN.finditer(content))
    if not matches:
        return _chunk_sliding_window(content, metadata)

    chunks: list[ChunkResult] = []

    # Preamble before first state
    preamble = content[:matches[0].start()].strip()
    if preamble and _count_tokens(preamble) >= MIN_CHUNK_TOKENS:
        chunks.append(ChunkResult(
            content=preamble,
            chunk_type="jurisdiction_preamble",
            chunk_index=0,
            metadata={**metadata, "jurisdiction_state": "general"},
        ))

    for i, match in enumerate(matches):
        state_name = match.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        segment = content[match.start():end].strip()
        if not segment:
            continue

        chunks.append(ChunkResult(
            content=segment,
            chunk_type="jurisdiction_rule",
            chunk_index=len(chunks),
            metadata={**metadata, "jurisdiction_state": state_name},
        ))

    return _enforce_size_limits(chunks)


# ---------------------------------------------------------------------------
# Strategy: sliding_window (fallback)
# ---------------------------------------------------------------------------


def _chunk_sliding_window(content: str, metadata: dict[str, Any]) -> list[ChunkResult]:
    """512-token windows with 64-token overlap, sentence-aligned."""
    encoder = _get_encoder()
    tokens = encoder.encode(content)
    total = len(tokens)

    if total <= MAX_CHUNK_TOKENS:
        return [ChunkResult(
            content=content.strip(),
            chunk_type="sliding_window",
            chunk_index=0,
            token_count=total,
            metadata=metadata,
        )]

    window_size = 512
    overlap = 64
    step = window_size - overlap

    chunks: list[ChunkResult] = []
    pos = 0

    while pos < total:
        end = min(pos + window_size, total)
        chunk_tokens = tokens[pos:end]
        chunk_text = encoder.decode(chunk_tokens)

        # Sentence-align the end: find last sentence boundary
        if end < total:
            last_period = chunk_text.rfind(". ")
            last_newline = chunk_text.rfind("\n")
            boundary = max(last_period, last_newline)
            if boundary > len(chunk_text) // 2:
                chunk_text = chunk_text[: boundary + 1].strip()

        if chunk_text.strip():
            chunks.append(ChunkResult(
                content=chunk_text.strip(),
                chunk_type="sliding_window",
                chunk_index=len(chunks),
                metadata=metadata,
            ))

        pos += step

    return _enforce_size_limits(chunks)


# ---------------------------------------------------------------------------
# Size enforcement: merge small chunks, split large ones
# ---------------------------------------------------------------------------


def _enforce_size_limits(chunks: list[ChunkResult]) -> list[ChunkResult]:
    """Merge chunks below MIN_CHUNK_TOKENS, split those above MAX_CHUNK_TOKENS."""
    result: list[ChunkResult] = []

    for chunk in chunks:
        tc = _count_tokens(chunk.content)
        chunk.token_count = tc

        if tc > MAX_CHUNK_TOKENS:
            # Split oversized chunk using sliding window
            sub_chunks = _chunk_sliding_window(chunk.content, chunk.metadata)
            for sc in sub_chunks:
                sc.chunk_type = chunk.chunk_type
                sc.parent_index = chunk.chunk_index
            result.extend(sub_chunks)
        elif tc < MIN_CHUNK_TOKENS and result:
            # Merge with previous chunk if combined size is within target
            prev = result[-1]
            combined_tokens = prev.token_count + tc
            if combined_tokens <= MAX_CHUNK_TOKENS:
                prev.content = prev.content + "\n\n" + chunk.content
                prev.token_count = combined_tokens
            else:
                result.append(chunk)
        else:
            result.append(chunk)

    # Re-index
    for i, chunk in enumerate(result):
        chunk.chunk_index = i

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_STRATEGY_MAP = {
    "clause_boundary": _chunk_clause_boundary,
    "api_endpoint": _chunk_api_endpoint,
    "template_spec": _chunk_template_spec,
    "jurisdiction_rule": _chunk_jurisdiction_rule,
    "sliding_window": _chunk_sliding_window,
}

VALID_STRATEGIES = frozenset(_STRATEGY_MAP.keys())


def chunk_document(
    content: str,
    strategy: str,
    metadata: dict[str, Any] | None = None,
) -> list[ChunkResult]:
    """Chunk a document using the specified strategy.

    Args:
        content: The document text to chunk.
        strategy: One of: clause_boundary, api_endpoint, template_spec,
                  jurisdiction_rule, sliding_window.
        metadata: Additional metadata to attach to each chunk.

    Returns:
        List of ChunkResult objects.

    Raises:
        ValueError: If strategy is unknown.
    """
    if not content or not content.strip():
        raise ValueError("Cannot chunk empty content")

    if strategy not in _STRATEGY_MAP:
        raise ValueError(
            f"Unknown chunking strategy '{strategy}'. "
            f"Valid strategies: {', '.join(sorted(VALID_STRATEGIES))}"
        )

    meta = dict(metadata) if metadata else {}
    meta["strategy"] = strategy

    fn = _STRATEGY_MAP[strategy]
    chunks = fn(content, meta)

    logger.info(
        "Chunked document: strategy=%s, chunks=%d, total_tokens=%d",
        strategy,
        len(chunks),
        sum(c.token_count for c in chunks),
    )

    return chunks
