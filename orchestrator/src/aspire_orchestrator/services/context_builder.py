"""Advisor Context Builder — Port from v1.5 context_builder.ts.

Builds advisor_context for Ava routing: infers mode, selects playbooks,
loads staff catalog. Used by param_extract_node to inject into LLM context.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"

# Module-level cache
_CACHE: dict[str, Any] = {}


def _safe_lower(s: Any) -> str:
    return str(s or "").lower()


def _load_once() -> None:
    """Load and cache playbook index + staff catalog."""
    if "playbook_index" not in _CACHE:
        idx_path = _CONFIG_DIR / "playbooks" / "index.json"
        if idx_path.exists():
            _CACHE["playbook_index"] = json.loads(idx_path.read_text(encoding="utf-8"))
        else:
            _CACHE["playbook_index"] = {"version": "unknown", "items": []}

    if "staff_catalog" not in _CACHE:
        cat_path = _CONFIG_DIR / "staff_catalog.json"
        if cat_path.exists():
            _CACHE["staff_catalog"] = json.loads(cat_path.read_text(encoding="utf-8"))
        else:
            _CACHE["staff_catalog"] = {"version": "unknown", "staff": []}

    if "playbook_text" not in _CACHE:
        _CACHE["playbook_text"] = {}


def infer_mode(task_type: str, payload_text: str) -> str:
    """Infer advisor mode from task_type and payload."""
    tt = _safe_lower(task_type)
    if tt.startswith("weekly.") or "weekly review" in payload_text or "end of week" in payload_text:
        return "weekly_review"
    if tt.startswith("daily.") or tt.startswith("ritual.") or any(
        kw in payload_text for kw in ("morning", "daily", "check-in")
    ):
        return "daily_pulse"
    if any(kw in payload_text for kw in ("overwhelmed", "stressed", "burned out", "exhausted", "behind")):
        return "load_shedding"
    return "default"


def _pick_playbooks(task_type: str, payload_text: str) -> list[str]:
    """Score and select 1-3 playbooks by match rules from index.json."""
    idx = _CACHE.get("playbook_index", {})
    items = idx.get("items", [])
    tt = _safe_lower(task_type)

    matches: list[tuple[str, int]] = []
    for item in items:
        score = 0
        match_rules = item.get("match", {})

        # task_type_prefix matching
        for prefix in match_rules.get("task_type_prefix", []):
            if tt.startswith(_safe_lower(prefix)):
                score += 3

        # any keyword matching
        for needle in match_rules.get("any", []):
            if _safe_lower(needle) in payload_text:
                score += 2

        if score > 0:
            matches.append((item["id"], score))

    # Always include consultant_loop as base layer
    chosen: list[str] = ["consultant_loop"]

    # Best match other than consultant_loop
    matches.sort(key=lambda x: x[1], reverse=True)
    for playbook_id, _score in matches:
        if playbook_id != "consultant_loop":
            chosen.append(playbook_id)
            break

    # Mode-specific hard preference
    mode = infer_mode(task_type, payload_text)
    if mode == "daily_pulse" and "daily_pulse" not in chosen:
        chosen.append("daily_pulse")
    if mode == "weekly_review" and "weekly_review" not in chosen:
        chosen.append("weekly_review")
    if mode == "load_shedding" and "load_shedding" not in chosen:
        chosen.append("load_shedding")

    # Dedup while preserving order, max 3
    seen: set[str] = set()
    result: list[str] = []
    for pb_id in chosen:
        if pb_id not in seen:
            seen.add(pb_id)
            result.append(pb_id)
    # Limit to 3 playbooks to keep LLM context size manageable (v1.5 constraint)
    return result[:3]


def _read_playbook_by_id(playbook_id: str) -> dict[str, str] | None:
    """Read playbook content by ID from index."""
    idx = _CACHE.get("playbook_index", {})
    items = idx.get("items", [])

    item = next((i for i in items if i["id"] == playbook_id), None)
    if not item:
        return None

    abs_path = _CONFIG_DIR / "playbooks" / item["path"]
    cache_key = str(abs_path)

    if cache_key not in _CACHE.get("playbook_text", {}):
        if abs_path.exists():
            _CACHE["playbook_text"][cache_key] = abs_path.read_text(encoding="utf-8")
        else:
            return None

    content = _CACHE["playbook_text"][cache_key]
    first_line = content.split("\n")[0] if content else ""
    title = first_line.lstrip("# ").strip() or playbook_id
    return {"id": playbook_id, "title": title, "content": content}


def build_advisor_context(
    task_type: str,
    payload: dict[str, Any] | None = None,
    suite_id: str = "",
) -> dict[str, Any]:
    """Build advisor_context for Ava routing prompt.

    Returns:
        {
            "version": "advisor_context_v1:...",
            "mode": "daily_pulse" | "weekly_review" | "load_shedding" | "default",
            "staff_catalog": {...},
            "playbooks": [...],
            "signals": {...}
        }
    """
    _load_once()

    safe_payload = payload or {}
    payload_text = _safe_lower(json.dumps(safe_payload, default=str))

    mode = infer_mode(task_type, payload_text)
    pb_ids = _pick_playbooks(task_type, payload_text)
    playbooks = [pb for pb_id in pb_ids if (pb := _read_playbook_by_id(pb_id)) is not None]

    # Extract signals from payload
    raw_open_loops = safe_payload.get("open_loops")
    open_loops_count = len(raw_open_loops) if isinstance(raw_open_loops, list) else None
    energy = safe_payload.get("energy")
    overload = bool(safe_payload.get("overload"))

    # Normalize staff catalog
    catalog = _CACHE.get("staff_catalog", {})
    staff = []
    for s in catalog.get("staff", []):
        staff.append({
            "name": str(s.get("name", "")),
            "lane": str(s.get("lane", "")),
            "chat_visible": bool(s.get("chat_visible")),
            "proposal_only": bool(s.get("proposal_only")),
            "implemented": bool(s.get("implemented")),
            "skillpack_id": str(s["skillpack_id"]) if s.get("skillpack_id") else None,
            "hard_rules": [str(r) for r in s["hard_rules"]] if isinstance(s.get("hard_rules"), list) else None,
            "gating": [str(g) for g in s["gating"]] if isinstance(s.get("gating"), list) else None,
            "default_risk_floor": str(s["default_risk_floor"]) if s.get("default_risk_floor") else None,
        })

    catalog_version = catalog.get("version", "unknown")
    index_version = _CACHE.get("playbook_index", {}).get("version", "unknown")

    # Financial knowledge context for Finn-related intents (graceful degradation)
    financial_knowledge = None
    _FINANCE_KEYWORDS = (
        "finance", "budget", "cash", "revenue", "expense", "payroll", "tax",
        "income", "profit", "loss", "invoice", "payment", "bank", "accounting",
        "bookkeeping", "write-off", "deduction", "plaid", "stripe", "quickbooks",
        "gusto", "adp", "forecast", "runway", "break-even", "depreciation",
        "1099", "w-2", "quarterly", "estimated payment",
    )
    if any(kw in payload_text for kw in _FINANCE_KEYWORDS):
        try:
            from aspire_orchestrator.services.financial_retrieval_service import get_financial_retrieval_service

            svc = get_financial_retrieval_service()
            user_text = safe_payload.get("text", "") or safe_payload.get("message", "") or ""
            if user_text:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    pass
                else:
                    rag_result = asyncio.run(svc.retrieve(user_text, suite_id=suite_id))
                    if rag_result and rag_result.chunks:
                        financial_knowledge = {
                            "chunk_count": len(rag_result.chunks),
                            "domains": list({c.get("domain") for c in rag_result.chunks if c.get("domain")}),
                            "context": svc.assemble_rag_context(rag_result),
                            "cache_hit": rag_result.cache_hit,
                        }
        except Exception as e:
            logger.warning("Financial knowledge context failed (non-fatal): %s", e)

    # Legal knowledge context for Clara-related intents (graceful degradation)
    legal_knowledge = None
    _LEGAL_KEYWORDS = ("contract", "nda", "legal", "clause", "sign", "agreement", "lease", "msa")
    if any(kw in payload_text for kw in _LEGAL_KEYWORDS):
        try:
            from aspire_orchestrator.services.legal_retrieval_service import get_retrieval_service

            svc = get_retrieval_service()
            # Extract user intent text for RAG query
            user_text = safe_payload.get("text", "") or safe_payload.get("message", "") or ""
            if user_text:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    # Can't await in sync context — skip RAG
                    pass
                else:
                    rag_result = asyncio.run(svc.retrieve(user_text, suite_id=suite_id))
                    if rag_result and rag_result.chunks:
                        legal_knowledge = {
                            "chunk_count": len(rag_result.chunks),
                            "domains": list({c.get("domain") for c in rag_result.chunks if c.get("domain")}),
                            "context": svc.assemble_rag_context(rag_result),
                            "cache_hit": rag_result.cache_hit,
                        }
        except Exception as e:
            logger.warning("Legal knowledge context failed (non-fatal): %s", e)

    result = {
        "version": f"advisor_context_v1:{catalog_version}:{index_version}",
        "versions": {"staff_catalog": catalog_version, "playbook_index": index_version},
        "mode": mode,
        "staff_catalog": {"version": catalog_version, "staff": staff},
        "playbooks": playbooks,
        "signals": {
            "open_loops_count": open_loops_count,
            "energy": energy if energy in ("high", "med", "low") else None,
            "overload": overload,
        },
    }
    if financial_knowledge:
        result["financial_knowledge"] = financial_knowledge
    if legal_knowledge:
        result["legal_knowledge"] = legal_knowledge
    return result
