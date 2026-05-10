"""
sync_receptionist_kb.py — Upload and attach receptionist KB docs to all 3 EL agents.

Walks Aspire-desktop/docs/agents/sarah-receptionist/kb/**/*.md recursively.
For each file:
  1. Validates required frontmatter (--strict default ON).
  2. Computes SHA256 of content.
  3. Checks if a KB doc with the same basename already exists in EL workspace.
  4. If exists and sha256 matches -> skip (idempotent).
  5. If missing or sha256 differs -> upload/update via EL API.
  6. Attaches each doc to all 3 receptionist agents with usage_mode: auto.
  7. Emits immutable receipts for every upload and every attach (Law #2).

Usage:
    EL_API_KEY=sk_... python scripts/sync_receptionist_kb.py
    EL_API_KEY=sk_... python scripts/sync_receptionist_kb.py --dry-run
    EL_API_KEY=sk_... python scripts/sync_receptionist_kb.py --no-strict

Aspire Laws:
    Law #2: every upload + attach emits a receipt (kb_doc_upload, kb_attach).
    Law #3: fail-closed on missing frontmatter in --strict mode.
    Law #9: EL API key sourced from env, never logged or emitted in receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure aspire_orchestrator is importable when run directly.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_PATH = _REPO_ROOT / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# KB root — relative to the workspace root (two levels up from backend/orchestrator)
_WORKSPACE_ROOT = _REPO_ROOT.parent.parent
KB_ROOT = _WORKSPACE_ROOT / "Aspire-desktop" / "docs" / "agents" / "sarah-receptionist" / "kb"

EL_API_BASE = "https://api.elevenlabs.io/v1"

# Three receptionist agents: (agent_id, display_name)
RECEPTIONIST_AGENTS: list[tuple[str, str]] = [
    ("agent_4801kqtapvsre2gb0gyb1ng631qr", "Tiffany"),
    ("agent_8901kmqdjnrte7psp6en4f85m4kt", "Sarah-FrontDesk"),
    ("agent_6501kp71h69jfqysgd055hemqhrq", "Sarah-Receptionist"),
]

# Required frontmatter keys per the Pass 5 spec
REQUIRED_FRONTMATTER_KEYS: frozenset[str] = frozenset({
    "title",
    "agent_scope",
    "priority",
    "business_type",
    "trade_scope",
    "last_reviewed",
    "sme_approved_by",
    "contract_version",
})

# Timeout for every EL HTTP call (Law #10 reliability: <5s default)
HTTP_TIMEOUT_SECONDS: int = 5


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _el_headers(api_key: str) -> dict[str, str]:
    return {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }


def _http_get(url: str, api_key: str) -> dict[str, Any]:
    """GET url and return parsed JSON body. Raises on HTTP error."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers=_el_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} GET {url}: {body[:400]}") from exc


def _http_post(url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST json body and return parsed JSON response. Raises on HTTP error."""
    import urllib.request
    import urllib.error

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=_el_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body_txt[:400]}") from exc


def _http_post_multipart(url: str, api_key: str, file_name: str, content: bytes) -> dict[str, Any]:
    """POST multipart/form-data file upload. Returns parsed JSON."""
    import urllib.request
    import urllib.error

    boundary = "----AspireKBBoundary" + uuid.uuid4().hex
    body_parts: list[bytes] = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(),
        b"Content-Type: text/markdown\r\n",
        b"\r\n",
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(body_parts)

    headers = {
        "xi-api-key": api_key,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} multipart POST {url}: {body_txt[:400]}") from exc


def _http_delete(url: str, api_key: str) -> None:
    """DELETE url. Raises on HTTP error (except 404 which is treated as idempotent)."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers={
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return  # Already deleted — idempotent
        body_txt = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} DELETE {url}: {body_txt[:400]}") from exc


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter block from markdown content.

    Returns (frontmatter_dict, body_without_frontmatter).
    Returns ({}, content) if no frontmatter block is found.
    """
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return {}, content

    lines = stripped.split("\n")
    end_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, content

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    fm: dict[str, Any] = {}
    for line in fm_lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Minimal YAML list parsing: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            fm[key] = [v.strip() for v in inner.split(",") if v.strip()]
        else:
            # Strip surrounding quotes
            fm[key] = value.strip("\"'")

    return fm, body


def _validate_frontmatter(path: Path, fm: dict[str, Any]) -> list[str]:
    """Return list of validation error messages. Empty = valid."""
    errors: list[str] = []
    for key in REQUIRED_FRONTMATTER_KEYS:
        if key not in fm or not fm[key]:
            errors.append(f"Missing required frontmatter key: {key}")
    return errors


# ---------------------------------------------------------------------------
# SHA256 helpers
# ---------------------------------------------------------------------------

def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# EL Knowledge Base API wrappers
# ---------------------------------------------------------------------------

def _list_kb_docs(api_key: str) -> list[dict[str, Any]]:
    """List all knowledge base documents in the EL workspace."""
    url = f"{EL_API_BASE}/convai/knowledge-base"
    try:
        resp = _http_get(url, api_key)
        # EL returns {"documents": [...]} or a list directly depending on version
        if isinstance(resp, list):
            return resp
        return resp.get("documents", resp.get("knowledge_bases", []))
    except RuntimeError as exc:
        logger.warning("Could not list KB docs (will treat all as new): %s", exc)
        return []


def _upload_kb_doc(api_key: str, file_name: str, content: bytes) -> str:
    """Upload a new KB document. Returns the doc_id."""
    url = f"{EL_API_BASE}/convai/knowledge-base/file"
    resp = _http_post_multipart(url, api_key, file_name, content)
    doc_id = resp.get("id") or resp.get("knowledge_base_id") or resp.get("document_id")
    if not doc_id:
        raise RuntimeError(f"EL did not return a doc ID for {file_name}. Response: {resp}")
    return str(doc_id)


def _delete_kb_doc(api_key: str, doc_id: str) -> None:
    """Delete an existing KB document (used for replace-on-sha-change)."""
    url = f"{EL_API_BASE}/convai/knowledge-base/{doc_id}"
    _http_delete(url, api_key)


def _attach_kb_doc_to_agent(
    api_key: str,
    agent_id: str,
    doc_id: str,
    usage_mode: str = "auto",
) -> None:
    """Attach a KB document to an agent."""
    url = f"{EL_API_BASE}/convai/agents/{agent_id}/knowledge-base"
    body = {
        "knowledge_base_id": doc_id,
        "usage_mode": usage_mode,
    }
    _http_post(url, api_key, body)


def _get_agent_kb_docs(api_key: str, agent_id: str) -> list[dict[str, Any]]:
    """Return list of KB docs currently attached to an agent."""
    url = f"{EL_API_BASE}/convai/agents/{agent_id}"
    try:
        agent = _http_get(url, api_key)
        # Navigate: agent.conversation_config.knowledge_base or .knowledge_bases
        conv_cfg = agent.get("conversation_config", {})
        kb = conv_cfg.get("knowledge_base", conv_cfg.get("knowledge_bases", []))
        if isinstance(kb, list):
            return kb
        return []
    except RuntimeError as exc:
        logger.warning("Could not fetch agent %s KB docs: %s", agent_id, exc)
        return []


# ---------------------------------------------------------------------------
# Receipt helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_upload_receipt(
    *,
    file_path: Path,
    file_sha256: str,
    doc_id: str,
    file_size: int,
    outcome: str,
    reason_code: str,
    agent_attachments: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": "kb_doc_upload",
        "action_type": "kb.doc.upload",
        "risk_tier": "GREEN",
        "outcome": outcome,
        "reason_code": reason_code,
        "actor_type": "SYSTEM",
        "actor_id": "sync_receptionist_kb",
        "created_at": _utc_now(),
        "redacted_inputs": {
            "file_basename": file_path.name,
            "file_size_bytes": file_size,
            "file_sha256": file_sha256,
            "dry_run": dry_run,
        },
        "redacted_outputs": {
            "doc_id": doc_id,
            "agent_attachments": agent_attachments,
        },
    }


def _make_attach_receipt(
    *,
    agent_id: str,
    agent_name: str,
    doc_id: str,
    usage_mode: str,
    outcome: str,
    reason_code: str,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": "kb_attach",
        "action_type": "kb.doc.attach",
        "risk_tier": "GREEN",
        "outcome": outcome,
        "reason_code": reason_code,
        "actor_type": "SYSTEM",
        "actor_id": "sync_receptionist_kb",
        "created_at": _utc_now(),
        "redacted_inputs": {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "doc_id": doc_id,
            "usage_mode": usage_mode,
            "dry_run": dry_run,
        },
        "redacted_outputs": {
            "attached": not dry_run and outcome == "success",
        },
    }


def _store_receipts(receipts: list[dict[str, Any]]) -> None:
    """Best-effort receipt persistence. Never raises (Law #2 coverage)."""
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        store_receipts(receipts)
    except Exception as exc:
        logger.warning("Receipt store unavailable (receipts logged only): %s", exc)
        for r in receipts:
            logger.info("RECEIPT %s", json.dumps(r))


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def _print_summary(rows: list[dict[str, Any]]) -> None:
    """Print a final summary table to stdout."""
    print(f"\n{'=' * 90}")
    print(f"  KB Sync Summary — {_utc_now()}")
    print(f"{'=' * 90}")
    header = f"{'File':<35} {'Size':>8} {'SHA256':>16} {'Upload':>12} {'Attached Agents':<30}"
    print(header)
    print("-" * 90)
    for row in rows:
        sha_short = row["sha256"][:12] + "..."
        agents_str = ", ".join(row["attached_agents"]) or "none"
        status_color = _GREEN if row["upload_status"] in ("uploaded", "skipped") else _RED
        print(
            f"{row['file']:<35} {row['size_bytes']:>8} {sha_short:>16} "
            f"{status_color}{row['upload_status']:>12}{_RESET} {agents_str:<30}"
        )
    print("=" * 90)

    total = len(rows)
    uploaded = sum(1 for r in rows if r["upload_status"] == "uploaded")
    skipped = sum(1 for r in rows if r["upload_status"] == "skipped")
    failed = sum(1 for r in rows if r["upload_status"] == "failed")
    dry_run_count = sum(1 for r in rows if r["upload_status"] == "dry_run")

    print(f"  Total: {total} | Uploaded: {uploaded} | Skipped: {skipped} | "
          f"Dry-run: {dry_run_count} | Failed: {failed}")
    print("=" * 90)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

class KBSyncResult:
    """Tracks per-file outcomes for the summary and exit code."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.all_receipts: list[dict[str, Any]] = []
        self.had_failure: bool = False
        self.frontmatter_failures: int = 0


def _sync_file(
    *,
    md_path: Path,
    api_key: str,
    existing_docs: dict[str, dict[str, Any]],  # basename -> {id, name}
    strict: bool,
    dry_run: bool,
    result: KBSyncResult,
) -> None:
    """Process one KB markdown file. Updates result in-place.

    existing_docs maps file basename -> EL doc info dict.
    """
    content_bytes = md_path.read_bytes()
    content_str = content_bytes.decode("utf-8", errors="replace")
    sha = _sha256(content_bytes)
    size = len(content_bytes)
    basename = md_path.name

    # 1. Frontmatter validation
    fm, _ = _parse_frontmatter(content_str)
    fm_errors = _validate_frontmatter(md_path, fm)
    if fm_errors and strict:
        logger.error(
            "Frontmatter validation FAILED for %s (--strict): %s",
            md_path.name,
            "; ".join(fm_errors),
        )
        failure_receipt = _make_upload_receipt(
            file_path=md_path,
            file_sha256=sha,
            doc_id="",
            file_size=size,
            outcome="failed",
            reason_code="FRONTMATTER_MISSING",
            agent_attachments=[],
            dry_run=dry_run,
        )
        result.all_receipts.append(failure_receipt)
        _store_receipts([failure_receipt])
        result.had_failure = True
        result.frontmatter_failures += 1
        result.rows.append({
            "file": basename,
            "size_bytes": size,
            "sha256": sha,
            "upload_status": "failed",
            "attached_agents": [],
        })
        return

    # 2. Idempotency check: existing doc with matching sha?
    existing = existing_docs.get(basename)
    doc_id: str = ""

    if existing:
        existing_sha = existing.get("sha256", "")
        if existing_sha == sha:
            # Idempotent skip — sha unchanged
            logger.info("Skipping %s (sha unchanged)", basename)
            doc_id = existing.get("id", "")
            upload_receipt = _make_upload_receipt(
                file_path=md_path,
                file_sha256=sha,
                doc_id=doc_id,
                file_size=size,
                outcome="success",
                reason_code="IDEMPOTENT_SKIP",
                agent_attachments=[aid for aid, _ in RECEPTIONIST_AGENTS],
                dry_run=dry_run,
            )
            result.all_receipts.append(upload_receipt)
            _store_receipts([upload_receipt])
            result.rows.append({
                "file": basename,
                "size_bytes": size,
                "sha256": sha,
                "upload_status": "skipped",
                "attached_agents": [name for _, name in RECEPTIONIST_AGENTS],
            })
            return
        else:
            # SHA changed — delete old doc and re-upload
            if not dry_run:
                old_id = existing.get("id", "")
                if old_id:
                    logger.info("Deleting outdated doc %s (sha changed)", old_id)
                    try:
                        _delete_kb_doc(api_key, old_id)
                    except RuntimeError as exc:
                        logger.warning("Could not delete old doc %s: %s", old_id, exc)

    # 3. Upload
    if dry_run:
        logger.info("DRY-RUN: would upload %s (%d bytes)", basename, size)
        upload_receipt = _make_upload_receipt(
            file_path=md_path,
            file_sha256=sha,
            doc_id="",
            file_size=size,
            outcome="success",
            reason_code="DRY_RUN",
            agent_attachments=[aid for aid, _ in RECEPTIONIST_AGENTS],
            dry_run=True,
        )
        result.all_receipts.append(upload_receipt)
        _store_receipts([upload_receipt])
        attach_receipts = [
            _make_attach_receipt(
                agent_id=agent_id,
                agent_name=agent_name,
                doc_id="",
                usage_mode="auto",
                outcome="success",
                reason_code="DRY_RUN",
                dry_run=True,
            )
            for agent_id, agent_name in RECEPTIONIST_AGENTS
        ]
        result.all_receipts.extend(attach_receipts)
        _store_receipts(attach_receipts)
        result.rows.append({
            "file": basename,
            "size_bytes": size,
            "sha256": sha,
            "upload_status": "dry_run",
            "attached_agents": [name for _, name in RECEPTIONIST_AGENTS],
        })
        return

    # Live upload
    try:
        doc_id = _upload_kb_doc(api_key, basename, content_bytes)
        logger.info("Uploaded %s -> doc_id=%s", basename, doc_id)
    except RuntimeError as exc:
        logger.error("Upload FAILED for %s: %s", basename, exc)
        failure_receipt = _make_upload_receipt(
            file_path=md_path,
            file_sha256=sha,
            doc_id="",
            file_size=size,
            outcome="failed",
            reason_code="PROVIDER_UPLOAD_ERROR",
            agent_attachments=[],
            dry_run=False,
        )
        result.all_receipts.append(failure_receipt)
        _store_receipts([failure_receipt])
        result.had_failure = True
        result.rows.append({
            "file": basename,
            "size_bytes": size,
            "sha256": sha,
            "upload_status": "failed",
            "attached_agents": [],
        })
        return

    # 4. Attach to all 3 agents
    attached_agent_ids: list[str] = []
    attached_agent_names: list[str] = []
    attach_receipts: list[dict[str, Any]] = []

    for agent_id, agent_name in RECEPTIONIST_AGENTS:
        try:
            _attach_kb_doc_to_agent(api_key, agent_id, doc_id, "auto")
            attached_agent_ids.append(agent_id)
            attached_agent_names.append(agent_name)
            logger.info("Attached %s to agent %s (%s)", doc_id, agent_id, agent_name)
            attach_receipts.append(
                _make_attach_receipt(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    doc_id=doc_id,
                    usage_mode="auto",
                    outcome="success",
                    reason_code="ATTACHED",
                    dry_run=False,
                )
            )
        except RuntimeError as exc:
            logger.error("Attach FAILED for doc %s -> agent %s: %s", doc_id, agent_id, exc)
            result.had_failure = True
            attach_receipts.append(
                _make_attach_receipt(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    doc_id=doc_id,
                    usage_mode="auto",
                    outcome="failed",
                    reason_code="PROVIDER_ATTACH_ERROR",
                    dry_run=False,
                )
            )

    result.all_receipts.extend(attach_receipts)
    _store_receipts(attach_receipts)

    upload_receipt = _make_upload_receipt(
        file_path=md_path,
        file_sha256=sha,
        doc_id=doc_id,
        file_size=size,
        outcome="success",
        reason_code="UPLOADED",
        agent_attachments=attached_agent_ids,
        dry_run=False,
    )
    result.all_receipts.append(upload_receipt)
    _store_receipts([upload_receipt])

    result.rows.append({
        "file": basename,
        "size_bytes": size,
        "sha256": sha,
        "upload_status": "uploaded",
        "attached_agents": attached_agent_names,
    })


def _build_existing_docs_index(api_key: str) -> dict[str, dict[str, Any]]:
    """Build basename -> {id, sha256, name} index from EL workspace docs."""
    docs = _list_kb_docs(api_key)
    index: dict[str, dict[str, Any]] = {}
    for doc in docs:
        # EL API returns docs with 'name' field matching the uploaded filename
        name = doc.get("name", "") or doc.get("file_name", "")
        if name:
            doc_id = doc.get("id") or doc.get("knowledge_base_id") or doc.get("document_id", "")
            index[name] = {
                "id": doc_id,
                "name": name,
                # EL may or may not return sha256; store empty string if absent
                "sha256": doc.get("sha256", doc.get("content_hash", "")),
            }
    return index


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Main entry point. Returns 0 on success, 1 on validation error, 2 on sync failure."""
    parser = argparse.ArgumentParser(description="Sync receptionist KB docs to ElevenLabs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and report without uploading or attaching.",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        default=False,
        help="Skip frontmatter validation (not recommended for production).",
    )
    args = parser.parse_args(argv)

    strict = not args.no_strict
    dry_run = args.dry_run

    # Fail closed: require EL API key (Law #3)
    api_key = os.environ.get("EL_API_KEY", "")
    if not api_key and not dry_run:
        logger.error("EL_API_KEY environment variable is required (Law #3: fail closed).")
        return 1

    if not KB_ROOT.is_dir():
        logger.error("KB root not found: %s", KB_ROOT)
        return 1

    # Collect all .md files recursively
    md_files = sorted(KB_ROOT.rglob("*.md"))
    logger.info("Found %d markdown files in %s", len(md_files), KB_ROOT)

    if not md_files:
        logger.error("No markdown files found in KB root: %s", KB_ROOT)
        return 1

    # Fetch existing EL KB docs to enable idempotency checks
    existing_docs: dict[str, dict[str, Any]] = {}
    if not dry_run and api_key:
        existing_docs = _build_existing_docs_index(api_key)
        logger.info("Found %d existing EL KB docs", len(existing_docs))

    result = KBSyncResult()

    for md_path in md_files:
        logger.info("Processing: %s", md_path.relative_to(KB_ROOT))
        try:
            _sync_file(
                md_path=md_path,
                api_key=api_key,
                existing_docs=existing_docs,
                strict=strict,
                dry_run=dry_run,
                result=result,
            )
        except Exception as exc:
            # Individual file failure: log receipt, continue with remaining files
            logger.error("Unexpected error processing %s: %s", md_path.name, exc)
            sha = _sha256(md_path.read_bytes())
            failure_receipt = _make_upload_receipt(
                file_path=md_path,
                file_sha256=sha,
                doc_id="",
                file_size=md_path.stat().st_size,
                outcome="failed",
                reason_code="UNEXPECTED_ERROR",
                agent_attachments=[],
                dry_run=dry_run,
            )
            result.all_receipts.append(failure_receipt)
            _store_receipts([failure_receipt])
            result.had_failure = True
            result.rows.append({
                "file": md_path.name,
                "size_bytes": md_path.stat().st_size,
                "sha256": sha,
                "upload_status": "failed",
                "attached_agents": [],
            })

    _print_summary(result.rows)

    if result.frontmatter_failures > 0:
        logger.error(
            "%d files failed frontmatter validation in --strict mode. Fix frontmatter and re-run.",
            result.frontmatter_failures,
        )
        return 2

    if result.had_failure:
        logger.error("One or more KB docs failed to upload or attach. Check logs above.")
        return 2

    total_receipts = len(result.all_receipts)
    logger.info(
        "KB sync complete. Files: %d | Receipts emitted: %d | Dry-run: %s",
        len(result.rows),
        total_receipts,
        dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
