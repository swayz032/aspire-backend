"""Skill Pack Manifest Loader — Phase 3 Wave 2.

Loads and validates skill pack manifests (JSON) from the config directory.
Manifests define each skill pack's identity, capabilities, risk profile,
and certification status.

Manifest fields:
  - skillpack_id: Unique identifier
  - name: Human-readable name
  - channel: internal_frontend | external | internal_backend
  - version: Semantic version
  - capabilities: List of capability strings
  - risk_profile: default_risk_tier, max_risk_tier
  - tools: List of tool IDs the pack can use
  - certification_status: uncertified | certified | revoked
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)


# =============================================================================
# Manifest Schema
# =============================================================================


_MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["skillpack_id", "name", "version"],
    "properties": {
        "skillpack_id": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "channel": {
            "type": "string",
            "enum": ["internal_frontend", "external", "internal_backend"],
        },
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "description": {"type": "string"},
        "capabilities": {
            "type": "array",
            "items": {"type": "string"},
        },
        "risk_profile": {
            "type": "object",
            "properties": {
                "default_risk_tier": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                },
                "max_risk_tier": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                },
            },
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
        },
        "providers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "certification_status": {
            "type": "string",
            "enum": ["uncertified", "certified", "revoked"],
        },
    },
    "additionalProperties": True,
}


# =============================================================================
# Loader
# =============================================================================


def load_manifest(filepath: str | Path) -> dict[str, Any]:
    """Load and validate a single skill pack manifest.

    Args:
        filepath: Path to the manifest JSON file.

    Returns:
        Validated manifest dictionary.

    Raises:
        FileNotFoundError: If the manifest file doesn't exist.
        jsonschema.ValidationError: If the manifest fails validation.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    jsonschema.validate(instance=manifest, schema=_MANIFEST_SCHEMA)

    logger.info(
        "Loaded manifest: %s v%s (status=%s)",
        manifest.get("skillpack_id"),
        manifest.get("version"),
        manifest.get("certification_status", "uncertified"),
    )

    return manifest


def load_all_manifests(directory: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load all skill pack manifests from the config directory.

    Args:
        directory: Path to the manifests directory.
                   Defaults to config/pack_manifests/.

    Returns:
        Dictionary mapping skillpack_id -> manifest dict.
    """
    if directory is None:
        directory = Path(__file__).parent.parent / "config" / "pack_manifests"

    dir_path = Path(directory)
    if not dir_path.exists():
        logger.warning("Manifests directory not found: %s", dir_path)
        return {}

    manifests: dict[str, dict[str, Any]] = {}

    for filepath in sorted(dir_path.glob("*.json")):
        # Skip the schema file itself
        if filepath.name == "manifest_schema.json":
            continue

        try:
            manifest = load_manifest(filepath)
            pack_id = manifest["skillpack_id"]
            manifests[pack_id] = manifest
        except Exception as e:
            logger.warning("Failed to load manifest %s: %s", filepath.name, e)

    logger.info("Loaded %d skill pack manifests", len(manifests))
    return manifests


def get_manifest_schema() -> dict[str, Any]:
    """Get the manifest validation schema."""
    return dict(_MANIFEST_SCHEMA)
