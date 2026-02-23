"""Ingest Legal Knowledge — Walk legal_knowledge/ and feed into Clara RAG pipeline.

Usage:
    python -m scripts.ingest_legal_knowledge [--dry-run] [--domain DOMAIN] [--verbose]
    python scripts/ingest_legal_knowledge.py [--dry-run] [--domain DOMAIN] [--verbose]

Walks the legal_knowledge/ directory tree, detects domain from subdirectory name,
and calls the ingestion pipeline for each file.

Domain detection:
  - Subdirectory name maps to domain: pandadoc_api, contract_law, business_context,
    compliance_risk
  - Metadata comment in file header (<!-- domain: X -->) overrides directory detection
  - Nested subdirectories (e.g., contract_law/jurisdiction_rules/) inherit parent domain

Law #2: Every ingestion produces receipts (created by the pipeline).
Law #3: Fails closed on missing files, empty content, or pipeline errors.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Allow running from project root or scripts/ dir
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aspire_orchestrator.services.legal_ingestion_pipeline import (
    IngestResult,
    ingest_file,
)

logger = logging.getLogger("ingest_legal_knowledge")

# Base path to legal knowledge files
_KNOWLEDGE_BASE = (
    Path(__file__).parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "legal_knowledge"
)

# Supported file extensions
_EXTENSIONS = {".md", ".txt", ".yaml", ".json"}

# Metadata domain pattern: <!-- domain: X -->
_DOMAIN_META_RE = re.compile(r"<!--\s*domain:\s*(\S+?)[\s,]", re.IGNORECASE)

# Subdirectory name to domain mapping
_DIR_DOMAIN_MAP: dict[str, str] = {
    "pandadoc_api": "pandadoc_api",
    "contract_law": "contract_law",
    "jurisdiction_rules": "contract_law",  # inherits parent domain
    "business_context": "business_context",
    "compliance_risk": "compliance_risk",
}


def _detect_domain(file_path: Path) -> str:
    """Detect the knowledge domain from file metadata or directory structure.

    Priority:
      1. Metadata comment in file header (<!-- domain: X -->)
      2. Subdirectory name mapping
      3. Fallback: "general"
    """
    # 1. Check file header for metadata domain
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            header = f.read(500)  # Read first 500 chars for metadata
        match = _DOMAIN_META_RE.search(header)
        if match:
            return match.group(1).strip()
    except OSError:
        pass

    # 2. Check directory name
    rel = file_path.relative_to(_KNOWLEDGE_BASE)
    for part in rel.parts:
        if part in _DIR_DOMAIN_MAP:
            return _DIR_DOMAIN_MAP[part]

    # 3. Fallback
    return "general"


def _collect_files(base_dir: Path, domain_filter: str | None = None) -> list[tuple[Path, str]]:
    """Collect all knowledge files with their detected domains.

    Returns list of (file_path, domain) tuples sorted by path.
    """
    if not base_dir.is_dir():
        logger.error("Knowledge base directory not found: %s", base_dir)
        return []

    files: list[tuple[Path, str]] = []
    for file_path in sorted(base_dir.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _EXTENSIONS:
            continue

        domain = _detect_domain(file_path)
        if domain_filter and domain != domain_filter:
            continue

        files.append((file_path, domain))

    return files


async def _ingest_all(
    files: list[tuple[Path, str]],
    dry_run: bool = False,
) -> list[IngestResult]:
    """Ingest all collected files through the pipeline.

    Args:
        files: List of (file_path, domain) tuples.
        dry_run: If True, only log what would be ingested without calling pipeline.

    Returns:
        List of IngestResult from the pipeline.
    """
    results: list[IngestResult] = []
    total = len(files)

    for idx, (file_path, domain) in enumerate(files, 1):
        logger.info(
            "[%d/%d] %s (domain=%s)%s",
            idx,
            total,
            file_path.name,
            domain,
            " [DRY RUN]" if dry_run else "",
        )

        if dry_run:
            results.append(IngestResult(
                source_path=str(file_path),
                domain=domain,
                chunks_created=0,
                chunks_skipped=0,
            ))
            continue

        try:
            result = await ingest_file(
                str(file_path),
                domain=domain,
                source_type="legal_knowledge",
            )
            results.append(result)
            logger.info(
                "  -> created=%d, skipped=%d, receipts=%d",
                result.chunks_created,
                result.chunks_skipped,
                len(result.receipt_ids),
            )
        except Exception as e:
            logger.error("  -> FAILED: %s", e)
            results.append(IngestResult(
                source_path=str(file_path),
                domain=domain,
                errors=[str(e)],
            ))

    return results


def _print_summary(results: list[IngestResult], dry_run: bool = False) -> None:
    """Print ingestion summary statistics."""
    total_created = sum(r.chunks_created for r in results)
    total_skipped = sum(r.chunks_skipped for r in results)
    total_errors = sum(len(r.errors) for r in results)
    total_receipts = sum(len(r.receipt_ids) for r in results)

    # Domain breakdown
    domain_stats: dict[str, dict[str, int]] = {}
    for r in results:
        d = r.domain
        if d not in domain_stats:
            domain_stats[d] = {"files": 0, "created": 0, "skipped": 0, "errors": 0}
        domain_stats[d]["files"] += 1
        domain_stats[d]["created"] += r.chunks_created
        domain_stats[d]["skipped"] += r.chunks_skipped
        domain_stats[d]["errors"] += len(r.errors)

    mode = "DRY RUN" if dry_run else "INGESTION"
    print(f"\n{'=' * 60}")
    print(f"LEGAL KNOWLEDGE {mode} SUMMARY")
    print(f"{'=' * 60}")
    print(f"Total files:    {len(results)}")
    print(f"Chunks created: {total_created}")
    print(f"Chunks skipped: {total_skipped} (dedup)")
    print(f"Errors:         {total_errors}")
    if not dry_run:
        print(f"Receipts:       {total_receipts}")

    print(f"\n{'Domain':<20} {'Files':>6} {'Created':>8} {'Skipped':>8} {'Errors':>7}")
    print(f"{'-' * 20} {'-' * 6} {'-' * 8} {'-' * 8} {'-' * 7}")
    for domain, stats in sorted(domain_stats.items()):
        print(
            f"{domain:<20} {stats['files']:>6} {stats['created']:>8} "
            f"{stats['skipped']:>8} {stats['errors']:>7}"
        )
    print(f"{'=' * 60}\n")

    if total_errors > 0:
        print("FILES WITH ERRORS:")
        for r in results:
            if r.errors:
                for err in r.errors:
                    print(f"  {r.source_path}: {err}")
        print()


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Ingest legal knowledge files into Clara RAG knowledge base",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be ingested without calling the pipeline",
    )
    parser.add_argument(
        "--domain",
        type=str,
        default=None,
        help="Only ingest files for a specific domain (e.g., contract_law, pandadoc_api)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="Override the knowledge base directory path",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    base_dir = Path(args.base_dir) if args.base_dir else _KNOWLEDGE_BASE

    # Collect files
    logger.info("Scanning knowledge base: %s", base_dir)
    files = _collect_files(base_dir, domain_filter=args.domain)

    if not files:
        logger.warning("No matching files found in %s", base_dir)
        return 1

    logger.info("Found %d files to ingest", len(files))

    if args.dry_run:
        print(f"\nDRY RUN — {len(files)} files would be ingested:\n")
        for file_path, domain in files:
            rel = file_path.relative_to(base_dir) if file_path.is_relative_to(base_dir) else file_path
            print(f"  [{domain:<20}] {rel}")

    # Run ingestion
    results = asyncio.run(_ingest_all(files, dry_run=args.dry_run))

    # Print summary
    _print_summary(results, dry_run=args.dry_run)

    # Exit code: 0 if no errors, 1 if any errors
    has_errors = any(r.errors for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
