"""Skill Pack Policy Loader — Phase 3 Wave 2.

Loads per-pack policy YAML files that define:
  - risk_policy: Risk tier rules for the pack
  - tool_policy: Which tools the pack can use
  - llm_policy: LLM model preferences
  - freshness_policy: Data freshness requirements (e.g., Adam's source freshness)
  - Additional pack-specific policies

Directory structure:
  config/pack_policies/
    adam/
      risk_policy.yaml
      tool_policy.yaml
      sources_policy.yaml
      freshness_policy.yaml
    quinn/
      risk_policy.yaml
      tool_policy.yaml
    ...
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def load_pack_policies(
    pack_id: str,
    directory: str | Path | None = None,
) -> dict[str, Any]:
    """Load all policy files for a skill pack.

    Args:
        pack_id: Skill pack identifier (e.g., "adam", "quinn", "finn")
        directory: Path to pack_policies directory. Defaults to config/pack_policies/

    Returns:
        Dictionary mapping policy_name -> policy data.
    """
    if directory is None:
        directory = Path(__file__).parent.parent / "config" / "pack_policies"

    dir_path = Path(directory)
    pack_dir = dir_path / pack_id

    if not pack_dir.exists():
        # Try with underscores/hyphens
        pack_dir = dir_path / pack_id.replace("-", "_")

    if not pack_dir.exists():
        pack_dir = dir_path / pack_id.replace("_", "-")

    if not pack_dir.exists():
        logger.debug("No policies directory for pack %s", pack_id)
        return {}

    policies: dict[str, Any] = {}

    for filepath in sorted(pack_dir.glob("*.yaml")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            policy_name = filepath.stem  # e.g., "risk_policy", "tool_policy"
            policies[policy_name] = data
            logger.debug("Loaded policy %s for pack %s", policy_name, pack_id)
        except Exception as e:
            logger.warning("Failed to load policy %s for pack %s: %s", filepath.name, pack_id, e)

    # Load markdown files (e.g., prompt_contract.md) as raw text
    for filepath in sorted(pack_dir.glob("*.md")):
        try:
            policy_name = filepath.stem
            policies[policy_name] = filepath.read_text(encoding="utf-8").strip()
            logger.debug("Loaded markdown policy %s for pack %s", policy_name, pack_id)
        except Exception as e:
            logger.warning("Failed to load policy %s for pack %s: %s", filepath.name, pack_id, e)

    logger.info("Loaded %d policies for pack %s", len(policies), pack_id)
    return policies


def load_all_pack_policies(directory: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load policies for all packs.

    Returns:
        Dictionary mapping pack_id -> {policy_name: policy_data}.
    """
    if directory is None:
        directory = Path(__file__).parent.parent / "config" / "pack_policies"

    dir_path = Path(directory)
    if not dir_path.exists():
        return {}

    all_policies: dict[str, dict[str, Any]] = {}

    for pack_dir in sorted(dir_path.iterdir()):
        if pack_dir.is_dir():
            pack_id = pack_dir.name
            policies = load_pack_policies(pack_id, directory=directory)
            if policies:
                all_policies[pack_id] = policies

    logger.info("Loaded policies for %d packs", len(all_policies))
    return all_policies


def get_risk_policy(pack_id: str, policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the risk policy for a pack.

    Returns the risk_policy if loaded, or a safe default.
    """
    if policies and "risk_policy" in policies:
        return policies["risk_policy"]

    loaded = load_pack_policies(pack_id)
    return loaded.get("risk_policy", {"default_tier": "green"})


def get_tool_policy(pack_id: str, policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the tool policy for a pack.

    Returns the tool_policy if loaded, or an empty allowlist (fail-closed).
    """
    if policies and "tool_policy" in policies:
        return policies["tool_policy"]

    loaded = load_pack_policies(pack_id)
    return loaded.get("tool_policy", {"allowed_tools": []})


def get_autonomy_policy(pack_id: str, policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the autonomy policy for a pack.

    Returns the autonomy policy if loaded, or bounded safe defaults.
    """
    if policies and "autonomy_policy" in policies:
        return policies["autonomy_policy"]

    loaded = load_pack_policies(pack_id)
    return loaded.get(
        "autonomy_policy",
        {
            "autonomy": {
                "auto_triage": True,
                "auto_draft": True,
                "auto_send": False,
                "max_agentic_iterations": 2,
                "max_retrieval_domains": 2,
                "timeout_ms": 4500,
            },
        },
    )


def get_observability_policy(pack_id: str, policies: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the observability policy for a pack."""
    if policies and "observability_policy" in policies:
        return policies["observability_policy"]

    loaded = load_pack_policies(pack_id)
    return loaded.get(
        "observability_policy",
        {
            "receipt_requirements": {
                "success": True,
                "denied": True,
                "failed": True,
            },
            "alerts": [],
            "metrics": [],
        },
    )


def get_prompt_contract(pack_id: str, policies: dict[str, Any] | None = None) -> str:
    """Get the prompt contract for a pack as raw markdown/text."""
    if policies and "prompt_contract" in policies:
        contract = policies["prompt_contract"]
        if isinstance(contract, str):
            return contract
        if isinstance(contract, dict):
            return str(contract.get("content", "")).strip()

    directory = Path(__file__).parent.parent / "config" / "pack_policies"
    for candidate in (
        directory / pack_id / "prompt_contract.md",
        directory / pack_id.replace("-", "_") / "prompt_contract.md",
        directory / pack_id.replace("_", "-") / "prompt_contract.md",
    ):
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning("Failed to read prompt_contract for pack %s: %s", pack_id, e)
                break
    return ""
