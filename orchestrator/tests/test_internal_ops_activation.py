from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.intent_classifier import IntentResult
from aspire_orchestrator.services.skill_router import SkillRouter


ROOT = Path(__file__).resolve().parent.parent
CONFIG_ROOT = ROOT / "src" / "aspire_orchestrator" / "config"


def test_internal_ops_registry_entries_are_active() -> None:
    data = yaml.safe_load((CONFIG_ROOT / "skill_pack_manifests.yaml").read_text(encoding="utf-8"))
    packs = data["skill_packs"]

    expected = {
        "sre_triage": "yellow",
        "qa_evals": "green",
        "security_review": "red",
        "release_manager": "yellow",
    }

    for pack_id, risk in expected.items():
        pack = packs[pack_id]
        assert pack["status"] == "active"
        assert pack["risk_tier"] == risk
        assert pack["category"] == "internal"


def test_internal_ops_manifests_are_internal_backend() -> None:
    manifest_dir = CONFIG_ROOT / "pack_manifests"
    expected_files = {
        "sre-triage.json": "sre_triage",
        "qa-evals.json": "qa_evals",
        "security-review.json": "security_review",
        "release-manager.json": "release_manager",
    }

    for file_name, registry_id in expected_files.items():
        manifest = json.loads((manifest_dir / file_name).read_text(encoding="utf-8"))
        assert manifest["registry_id"] == registry_id
        assert manifest["channel"] == "internal_backend"
        assert manifest["internal_only"] is True


@pytest.mark.asyncio
async def test_internal_ops_are_blocked_from_user_facing_routing() -> None:
    router = SkillRouter()
    intent = IntentResult(
        action_type="security.scan.execute",
        skill_pack="security_review",
        confidence=0.95,
        entities={},
        risk_tier=RiskTier.RED,
        requires_clarification=False,
    )

    plan = await router.route(
        intent,
        context={"current_agent": "ava", "allow_internal_routing": False},
    )

    assert plan.steps == []
    assert plan.deny_reason == "internal_only_pack"


@pytest.mark.asyncio
async def test_internal_ops_allow_admin_bridge_routing() -> None:
    router = SkillRouter()
    intent = IntentResult(
        action_type="security.scan.execute",
        skill_pack="security_review",
        confidence=0.95,
        entities={},
        risk_tier=RiskTier.RED,
        requires_clarification=False,
    )

    plan = await router.route(
        intent,
        context={"current_agent": "ava_admin", "allow_internal_routing": True},
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].skill_pack == "security_review"
    assert plan.deny_reason is None
