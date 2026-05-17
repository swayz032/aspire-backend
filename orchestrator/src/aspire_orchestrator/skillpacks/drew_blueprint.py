# [STATUS: v1-active, BYPASS] — Reachable via /v1/agents/invoke-sync.
# Bypasses LangGraph: no policy gate, no token mint, no central receipt audit.
# Migration debt — route through /v1/intents in a later wave.
"""Drew Blueprint Story Engine — Wave 1A skeleton.

Reads architectural blueprints, builds a multi-discipline understanding, and produces
a phase-by-phase build narrative with line-item materials. This module is the Wave 1A
SKELETON: dispatch shape + receipts only — all stages return `status: "stub"`.

Pipeline tasks (orchestrator-driven, Drew never decides):
    INGEST   → parse PDFs (LlamaParse primary, Azure Doc Intel fallback)
    CLASSIFY → assign discipline + sheet metadata
    SEE      → vision pass for symbols / bbox / confidence
    REASON   → derive assemblies, materials, story-by-phase
    PROCURE  → push material requests to supplier playbooks

Law compliance:
  - Law #1: Skill pack runs bounded tasks; orchestrator decides invocation.
  - Law #2: Every method emits a receipt via _emit_receipt.
  - Law #3: Fails closed on unknown task or missing prompt/model env in production.
  - Law #7: No autonomous decisions; tasks are dispatched in by name.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

PROMPT_PATH = (
    Path(__file__).parent.parent
    / "services"
    / "blueprint"
    / "prompts"
    / "drew_system_prompt.md"
)

ACTOR_DREW = "skillpack:drew-blueprint"
RECEIPT_VERSION = "1.0"


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """SHA256 of canonicalized inputs for receipt linkage (mirrors Adam)."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _build_receipt(
    *,
    correlation_id: str,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Drew receipt aligned with Adam's shape (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": ACTOR_DREW,
        "correlation_id": correlation_id,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow" if status != "denied" else "deny",
            "policy_id": "drew-blueprint-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class Drew:
    """Drew skill pack — blueprint ingestion + story generation."""

    actor: str = "drew"

    def __init__(self) -> None:
        if not PROMPT_PATH.exists():
            raise RuntimeError(f"Drew system prompt missing: {PROMPT_PATH}")
        self.system_prompt: str = PROMPT_PATH.read_text(encoding="utf-8")

        env = os.getenv("ASPIRE_ENV", "development")
        if env == "production":
            model = os.getenv("ASPIRE_DREW_MODEL_PROD") or os.getenv("DREW_MODEL_PROD")
            if not model:
                raise RuntimeError(
                    "ASPIRE_DREW_MODEL_PROD env var required in production (Law #3)",
                )
            self.model: str = model
        else:
            self.model = (
                os.getenv("ASPIRE_DREW_MODEL_DEV")
                or os.getenv("DREW_MODEL_DEV")
                or "gpt-5.4-mini"
            )

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    def run_agentic_loop(
        self,
        task: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        """Route an inbound task to its handler. Fails closed on unknown task."""
        dispatch: dict[str, Any] = {
            "INGEST": self.ingest,
            "CLASSIFY": self.classify,
            "SEE": self.see,
            "REASON": self.reason,
            "PROCURE": self.procure,
        }
        handler = dispatch.get(task)
        if handler is None:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="drew.unknown_task",
                status="denied",
                inputs={"task": task},
            )
            return {"status": "deny", "reason": f"unknown task: {task}"}
        return handler(payload, correlation_id)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Stage stubs (Wave 1A — implementations land in Waves 2-5)
    # ------------------------------------------------------------------
    def ingest(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.ingest",
            status="stub",
            inputs={"task": "INGEST"},
        )
        return {"status": "stub", "stage": "ingest"}

    def classify(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.classify",
            status="stub",
            inputs={"task": "CLASSIFY"},
        )
        return {"status": "stub", "stage": "classify"}

    def see(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.see",
            status="stub",
            inputs={"task": "SEE"},
        )
        return {"status": "stub", "stage": "see"}

    def reason(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.reason",
            status="stub",
            inputs={"task": "REASON"},
        )
        return {"status": "stub", "stage": "reason"}

    def procure(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.procure",
            status="stub",
            inputs={"task": "PROCURE"},
        )
        return {"status": "stub", "stage": "procure"}

    # ------------------------------------------------------------------
    # Receipt emission (Law #2)
    # ------------------------------------------------------------------
    def _emit_receipt(
        self,
        *,
        correlation_id: str,
        event_type: str,
        status: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt = _build_receipt(
            correlation_id=correlation_id,
            event_type=event_type,
            status=status,
            inputs=inputs,
            metadata=metadata,
        )
        store_receipts([receipt])
        return receipt
