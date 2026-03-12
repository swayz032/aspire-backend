from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "scaffold_agent.py"
spec = importlib.util.spec_from_file_location("scaffold_agent", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def test_mail_ops_desk_validates() -> None:
    target = module.build_validation_target(
        ROOT,
        type("Args", (), {"registry_id": "mail_ops_desk", "owner_key": None, "manifest_id": "mail-ops-desk"})(),
    )
    problems = module.validate_agent(ROOT, target)
    assert problems == []


def test_mail_ops_desk_certifies() -> None:
    target = module.build_validation_target(
        ROOT,
        type("Args", (), {"registry_id": "mail_ops_desk", "owner_key": None, "manifest_id": "mail-ops-desk"})(),
    )
    problems = module.certify_agent(ROOT, target)
    assert problems == []
