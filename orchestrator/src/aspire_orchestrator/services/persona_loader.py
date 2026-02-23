"""Skill Pack Persona Loader — Phase 3 Wave 2.

Loads persona/system_prompt.md files for each skill pack and injects
them into the OpenAI system message.

Each skill pack has a persona file that defines:
  - Role identity (who the agent is)
  - Communication style
  - Domain constraints
  - Governance reminders (Laws they must follow)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_persona(pack_id: str, directory: str | Path | None = None) -> str | None:
    """Load the persona/system prompt for a skill pack.

    Args:
        pack_id: Skill pack identifier (e.g., "adam-research", "quinn-invoicing")
        directory: Path to personas directory. Defaults to config/pack_personas/

    Returns:
        The persona text, or None if not found.
    """
    if directory is None:
        directory = Path(__file__).parent.parent / "config" / "pack_personas"

    dir_path = Path(directory)
    if not dir_path.exists():
        logger.debug("Personas directory not found: %s", dir_path)
        return None

    # Try multiple filename patterns
    candidates = [
        dir_path / f"{pack_id}_system_prompt.md",
        dir_path / f"{pack_id.replace('-', '_')}_system_prompt.md",
        dir_path / f"{pack_id}.md",
        dir_path / f"{pack_id.replace('-', '_')}.md",
    ]

    for filepath in candidates:
        if filepath.exists():
            try:
                text = filepath.read_text(encoding="utf-8").strip()
                if text:
                    logger.info("Loaded persona for %s from %s", pack_id, filepath.name)
                    return text
            except Exception as e:
                logger.warning("Failed to read persona %s: %s", filepath.name, e)

    logger.debug("No persona found for pack %s", pack_id)
    return None


def load_all_personas(directory: str | Path | None = None) -> dict[str, str]:
    """Load all available persona files.

    Returns:
        Dictionary mapping pack_id -> persona text.
    """
    if directory is None:
        directory = Path(__file__).parent.parent / "config" / "pack_personas"

    dir_path = Path(directory)
    if not dir_path.exists():
        return {}

    personas: dict[str, str] = {}

    for filepath in sorted(dir_path.glob("*.md")):
        try:
            text = filepath.read_text(encoding="utf-8").strip()
            if text:
                # Derive pack_id from filename
                pack_id = filepath.stem.replace("_system_prompt", "")
                personas[pack_id] = text
        except Exception as e:
            logger.warning("Failed to read persona %s: %s", filepath.name, e)

    logger.info("Loaded %d personas", len(personas))
    return personas


def load_prompt_pack(
    surface: str,
    version: str | None = None,
) -> dict[str, str]:
    """Load a 4-part Ava prompt pack.

    Version resolution:
    1. Check env: AVA_{SURFACE}_PROMPT_VERSION (e.g. AVA_USER_PROMPT_VERSION=v1.3.0)
    2. If version provided, load from config/prompt_sets/{surface}/{version}/
    3. Fallback: config/prompt_packs/{surface}/ (latest)

    Returns:
        {"system": "...", "persona": "...", "constraints": "...", "fewshots": "..."}

    Raises:
        FileNotFoundError if system.md is missing (fail-closed — Law #3)
    """
    import os

    base_dir = Path(__file__).parent.parent / "config"

    # Normalize surface name
    clean_surface = surface.replace("ava_", "").replace("ava-", "")
    if clean_surface not in ("user", "admin"):
        clean_surface = "user"

    # Version resolution
    env_key = f"AVA_{clean_surface.upper()}_PROMPT_VERSION"
    resolved_version = version if version is not None else os.environ.get(env_key)

    if resolved_version:
        pack_dir = base_dir / "prompt_sets" / clean_surface / resolved_version
    else:
        pack_dir = base_dir / "prompt_packs" / clean_surface

    parts = {}
    for part_name in ("system", "persona", "constraints", "fewshots"):
        filepath = pack_dir / f"{part_name}.md"
        if filepath.exists():
            try:
                text = filepath.read_text(encoding="utf-8").strip()
                if text:
                    parts[part_name] = text
            except Exception as e:
                logger.warning("Failed to read prompt pack part %s: %s", filepath.name, e)

    # Fail-closed: system.md is required
    if "system" not in parts:
        raise FileNotFoundError(
            f"Required system.md not found in {pack_dir} (Law #3: fail closed)"
        )

    logger.info(
        "Loaded prompt pack: surface=%s, version=%s, parts=%s",
        clean_surface, resolved_version or "latest", list(parts.keys()),
    )
    return parts
