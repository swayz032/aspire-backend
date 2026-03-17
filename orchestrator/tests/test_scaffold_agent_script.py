from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "scaffold_agent.py"
spec = importlib.util.spec_from_file_location("scaffold_agent", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ScaffoldAgentScriptTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        (root / "src" / "aspire_orchestrator" / "config" / "pack_manifests").mkdir(parents=True)
        (root / "src" / "aspire_orchestrator" / "config" / "pack_personas").mkdir(parents=True)
        (root / "src" / "aspire_orchestrator" / "config" / "pack_policies").mkdir(parents=True)
        (root / "src" / "aspire_orchestrator" / "skillpacks").mkdir(parents=True)
        (root / "src" / "aspire_orchestrator" / "nodes").mkdir(parents=True)
        (root / "src" / "aspire_orchestrator" / "services").mkdir(parents=True)
        (root / "tests").mkdir()

        (root / "src" / "aspire_orchestrator" / "config" / "skill_pack_manifests.yaml").write_text(
            'version: "1.0.0"\nupdated_at: "2026-03-11"\ndefaults:\n  per_suite_enabled: true\n\nskill_packs:\n\ntools:\n',
            encoding="utf-8",
        )
        (root / "src" / "aspire_orchestrator" / "config" / "policy_matrix.yaml").write_text(
            'version: "1.0.0"\nupdated_at: "2026-03-11"\ndefaults:\n  deny_by_default: true\n\nactions:\n',
            encoding="utf-8",
        )
        (root / "src" / "aspire_orchestrator" / "services" / "agent_identity.py").write_text(
            'AGENT_PERSONA_MAP: dict[str, str] = {\n    "ava": "ava_user_system_prompt.md",\n}\n\n# Agent display names\n_AGENT_DISPLAY_NAMES = {}\n',
            encoding="utf-8",
        )

    def test_build_spec_uses_banking_preset_defaults(self) -> None:
        args = SimpleNamespace(
            agent_name="Blake",
            role_title=None,
            domain=None,
            actions=None,
            preset="banking",
            owner_key=None,
            registry_id=None,
            manifest_id=None,
            category=None,
            provider=None,
            role_description=None,
            description=None,
            tone=None,
            prompt_style=None,
            observability_tags=None,
            no_memory=False,
        )
        built = module.build_spec(args)
        self.assertEqual(built.role_title, "Banking Operations Specialist")
        self.assertEqual(built.domain, "banking")
        self.assertEqual(
            built.actions,
            ["accounts.read", "transactions.review", "transfer.prepare"],
        )
        self.assertEqual(built.category, "finance")
        self.assertEqual(built.prompt_style, "operational")
        self.assertEqual(built.observability_tags, ["banking", "finance", "receipts"])

    def test_scaffold_agent_writes_files_and_updates_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)

            scaffold_spec = module.ScaffoldSpec(
                agent_name="Blake",
                role_title="Banking Specialist",
                domain="banking",
                actions=["banking.read", "banking.write"],
                owner_key="blake",
                registry_id="blake_banking",
                manifest_id="blake-banking",
                category="internal",
                provider="internal",
                role_description="Banking Specialist",
                description="Banking workflows",
                tone="direct",
                memory_enabled=True,
                prompt_style="operational",
                preset_name="banking",
                observability_tags=["banking", "finance"],
            )

            created = module.scaffold_agent(root, scaffold_spec)
            self.assertEqual(len(created), 9)

            policy_dir = root / "src" / "aspire_orchestrator" / "config" / "pack_policies" / "blake_banking"
            for file_name in (
                "risk_policy.yaml",
                "tool_policy.yaml",
                "autonomy_policy.yaml",
                "observability_policy.yaml",
                "prompt_contract.md",
            ):
                self.assertTrue((policy_dir / file_name).exists(), file_name)

            registry_text = (root / "src" / "aspire_orchestrator" / "config" / "skill_pack_manifests.yaml").read_text(encoding="utf-8")
            self.assertIn("blake_banking:", registry_text)
            policy_text = (root / "src" / "aspire_orchestrator" / "config" / "policy_matrix.yaml").read_text(encoding="utf-8")
            self.assertIn("banking.read:", policy_text)
            self.assertIn("banking.write:", policy_text)
            identity_text = (root / "src" / "aspire_orchestrator" / "services" / "agent_identity.py").read_text(encoding="utf-8")
            self.assertIn('"blake": "blake_banking_system_prompt.md"', identity_text)

    def test_validate_succeeds_for_scaffolded_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            args = SimpleNamespace(
                agent_name="Ari",
                role_title=None,
                domain=None,
                actions=None,
                preset="legal",
                owner_key=None,
                registry_id=None,
                manifest_id=None,
                category=None,
                provider=None,
                role_description=None,
                description=None,
                tone=None,
                prompt_style=None,
                observability_tags=None,
                no_memory=False,
            )
            built = module.build_spec(args)
            module.scaffold_agent(root, built)

            exit_code = module.main(["validate", "--root", str(root)])
            self.assertEqual(exit_code, 0)

    def test_validate_fails_when_required_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            args = SimpleNamespace(
                agent_name="Mina",
                role_title="Healthcare Specialist",
                domain="healthcare",
                actions="read,prepare",
                preset=None,
                owner_key=None,
                registry_id=None,
                manifest_id=None,
                category="internal",
                provider="internal",
                role_description=None,
                description=None,
                tone="calm",
                prompt_style="compliance-first",
                observability_tags="healthcare,audit",
                no_memory=False,
            )
            built = module.build_spec(args)
            module.scaffold_agent(root, built)

            missing = root / "src" / "aspire_orchestrator" / "config" / "pack_policies" / built.registry_id / "tool_policy.yaml"
            missing.unlink()

            exit_code = module.main(["validate", "--root", str(root)])
            self.assertEqual(exit_code, 1)
            ok, failures = module.run_validate(root)
            self.assertFalse(ok)
            self.assertTrue(any("missing tool_policy" in failure for failure in failures))

    def test_certify_succeeds_for_scaffolded_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            built = module.build_spec(
                SimpleNamespace(
                    agent_name="Nia",
                    role_title=None,
                    domain=None,
                    actions=None,
                    preset="banking",
                    owner_key=None,
                    registry_id=None,
                    manifest_id=None,
                    category=None,
                    provider=None,
                    role_description=None,
                    description=None,
                    tone=None,
                    prompt_style=None,
                    observability_tags=None,
                    no_memory=False,
                )
            )
            module.scaffold_agent(root, built)
            exit_code = module.main(["certify", "--root", str(root), "--registry-id", built.registry_id])
            self.assertEqual(exit_code, 0)

    def test_build_validation_target_uses_manifest_owner_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            built = module.ScaffoldSpec(
                agent_name="Mail Ops",
                role_title="Mail Operations Specialist",
                domain="desk",
                actions=["domain.check"],
                owner_key="mail_ops",
                registry_id="mail_ops_desk",
                manifest_id="mail-ops-desk",
                category="internal",
                provider="internal",
                role_description="Mail Operations Specialist",
                description="Mail operations workflows",
                tone="direct",
                memory_enabled=True,
                prompt_style="operational",
                preset_name=None,
                observability_tags=["mail", "domain"],
            )
            module.scaffold_agent(root, built)

            target = module.build_validation_target(
                root,
                SimpleNamespace(registry_id="mail_ops_desk", owner_key=None, manifest_id="mail-ops-desk"),
            )

            self.assertEqual(target.owner_key, "mail_ops")


if __name__ == "__main__":
    unittest.main()
