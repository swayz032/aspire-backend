"""Skill Pack Factory — Load manifests → register in orchestrator.

Reads skill_pack_manifests.yaml and creates SkillPackWorker instances
that the orchestrator can route to. This is the bridge between the
YAML configuration and the runtime skill pack registry.

Law compliance:
- Law #1: Factory does not decide — it only registers
- Law #2: Factory registration produces a receipt
- Law #3: Invalid manifests are rejected (fail-closed)
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillPackRegistration:
    """Result of registering a skill pack."""

    pack_id: str
    name: str
    owner: str
    actions: list[str]
    tools: list[str]
    risk_tier: str
    registered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class FactoryResult:
    """Result of loading all skill packs."""

    registered: list[SkillPackRegistration]
    failed: list[dict[str, Any]]  # { pack_id, error }
    receipt: dict[str, Any]


class SkillPackFactory:
    """Load and register skill packs from manifests.

    The factory is pure plumbing (Law #7) — it reads YAML, validates required
    fields, and returns structured registrations.  It never decides which pack
    to route a request to; that is the orchestrator's job (Law #1).
    """

    def __init__(self, manifest_path: str | None = None):
        self._manifest_path = manifest_path or os.path.join(
            os.path.dirname(__file__), "..", "config", "skill_pack_manifests.yaml"
        )

    def load_all(self) -> FactoryResult:
        """Load all skill pack manifests and register them.

        Returns FactoryResult with registered packs, failures, and receipt.
        Every invocation produces a receipt (Law #2).
        """
        # Read YAML
        with open(self._manifest_path) as f:
            data = yaml.safe_load(f)

        packs = data.get("skill_packs", {})
        registered: list[SkillPackRegistration] = []
        failed: list[dict[str, Any]] = []

        for pack_id, pack_data in packs.items():
            try:
                reg = self._register_pack(pack_id, pack_data)
                registered.append(reg)
            except Exception as e:
                failed.append({"pack_id": pack_id, "error": str(e)})
                logger.warning("Failed to register pack %s: %s", pack_id, e)

        receipt = {
            "id": str(uuid.uuid4()),
            "action_type": "factory.load_all",
            "outcome": "success" if not failed else "partial",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "registered_count": len(registered),
                "failed_count": len(failed),
                "pack_ids": [r.pack_id for r in registered],
            },
        }

        return FactoryResult(registered=registered, failed=failed, receipt=receipt)

    def _register_pack(
        self, pack_id: str, pack_data: dict[str, Any]
    ) -> SkillPackRegistration:
        """Register a single skill pack from manifest data.

        Fail-closed (Law #3): missing required fields raise ValueError.
        """
        required_fields = ["name", "owner"]
        for field_name in required_fields:
            if field_name not in pack_data:
                raise ValueError(f"Missing required field: {field_name}")

        return SkillPackRegistration(
            pack_id=pack_id,
            name=pack_data["name"],
            owner=pack_data["owner"],
            actions=pack_data.get("actions", []),
            tools=pack_data.get("tools", []),
            risk_tier=pack_data.get("risk_tier", "green"),
        )
