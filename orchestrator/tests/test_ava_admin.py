from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.ava_admin import AvaAdminSkillPack

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / 'scripts' / 'scaffold_agent.py'
spec = importlib.util.spec_from_file_location('scaffold_agent', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ava_admin_validates() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_admin', 'owner_key': None, 'manifest_id': 'ava-admin'})(),
    )
    problems = module.validate_agent(ROOT, target)
    assert problems == []


def test_ava_admin_certifies() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_admin', 'owner_key': None, 'manifest_id': 'ava-admin'})(),
    )
    problems = module.certify_agent(ROOT, target)
    assert problems == []


@pytest.mark.asyncio
async def test_ava_admin_health_pulse_wrapper() -> None:
    pack = AvaAdminSkillPack()
    ctx = AgentContext(suite_id='system', office_id='system', correlation_id='corr-ava-admin')
    result = await pack.admin_ops_health_pulse({}, ctx)
    assert result.success is True
    assert result.data['voice_id'] == '56bWURjYFHyYyVf490Dp'
