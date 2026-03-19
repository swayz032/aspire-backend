from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.ava_user import AvaUserSkillPack

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / 'scripts' / 'scaffold_agent.py'
spec = importlib.util.spec_from_file_location('scaffold_agent', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)
MANIFEST_PATH = ROOT / 'src' / 'aspire_orchestrator' / 'config' / 'pack_manifests' / 'ava-user.json'


def test_ava_user_validates() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_user', 'owner_key': None, 'manifest_id': 'ava-user'})(),
    )
    problems = module.validate_agent(ROOT, target)
    assert problems == []


def test_ava_user_certifies() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_user', 'owner_key': None, 'manifest_id': 'ava-user'})(),
    )
    problems = module.certify_agent(ROOT, target)
    assert problems == []


def test_ava_user_manifest_channel_is_external() -> None:
    assert '"channel": "external"' in MANIFEST_PATH.read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_ava_user_governance_preview_receipt() -> None:
    pack = AvaUserSkillPack()
    ctx = AgentContext(suite_id='STE-0001', office_id='OFF-0001', correlation_id='corr-ava-user')
    result = await pack.governance_preview({'action_type': 'email.send'}, ctx)
    assert result.success is True
    assert result.data['governance']['approval_required'] is True
    assert result.receipt['event_type'] == 'governance.preview'
