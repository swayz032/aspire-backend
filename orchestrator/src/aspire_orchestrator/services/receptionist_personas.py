"""Receptionist persona registry — source of truth for Sarah/Tiffany.

Each tenant picks ONE persona on Front Desk Setup; the chosen slug is stored
in `front_desk_configs.receptionist_persona` (migration 109). This module
maps slug -> the rest of the persona contract:

  - agent_id     : ElevenLabs agent the inbound number is attached to
  - voice_id     : the voice the agent speaks with (display only — EL uses
                   the agent's own voice config; this is for transparency)
  - display_name : what the UI / Sarah-Status-Rail / receipts render
  - role_label   : "AI Front Desk Agent" by default (consistent across personas)
  - headshot_url : 240x240 PNG served by Aspire-desktop static host
                   (`/personas/<slug>.png`). Optional — UI falls back to
                   initials + accent color when missing.
  - preview_url  : pre-rendered 12-second greeting MP3 served at
                   `/personas/<slug>.mp3`. Same voice the agent uses on live
                   calls. Static asset — zero per-preview EL cost.
  - accent_color : hex, drives the picker card highlight + initials chip
                   color when no headshot is rendered.
  - description  : short marketing-style sentence shown under display_name
                   in the Front Desk Setup picker card.

Adding a 3rd persona:
  1) Append a new entry to `_PERSONAS` (slug + agent_id + voice_id + copy).
  2) Add `<slug>.mp3` and (optionally) `<slug>.png` to
     Aspire-desktop/public/personas/.
  3) Update migration 109's CHECK constraint to allow the new slug.
  4) Done — frontend reads the registry via GET /v1/front-desk/personas.

Law compliance:
  Law #1  — only the orchestrator decides agent attachment; this module just
            exposes the static map.
  Law #6  — the registry is tenant-agnostic; per-tenant choice lives in
            front_desk_configs (RLS-isolated).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class ReceptionistPersona:
    slug: str
    agent_id: str
    voice_id: str
    display_name: str
    role_label: str
    headshot_url: str
    preview_url: str
    accent_color: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Default persona slug — used when a tenant has no row yet (purchase before
# Setup save) and as the migration default.
DEFAULT_PERSONA_SLUG: str = "sarah"


# IMPORTANT: agent_id / voice_id values are verified live (2026-05-04) via
# `mcp__elevenlabs__list_agents` + `mcp__elevenlabs__get_agent`. Re-verify when
# duplicating to a third persona, changing voices, or migrating EL workspaces.
_PERSONAS: tuple[ReceptionistPersona, ...] = (
    ReceptionistPersona(
        slug="sarah",
        agent_id="agent_6501kp71h69jfqysgd055hemqhrq",
        voice_id="3dzJXoCYueSQiptQ6euE",
        display_name="Sarah",
        role_label="AI Front Desk Agent",
        headshot_url="/personas/sarah.png",
        preview_url="/personas/sarah.mp3",
        accent_color="#22D3EE",
        description="Calm, polished, and reliable — the trusted voice that picks up first.",
    ),
    ReceptionistPersona(
        slug="tiffany",
        agent_id="agent_4801kqtapvsre2gb0gyb1ng631qr",
        voice_id="6aDn1KB0hjpdcocrUkmq",
        display_name="Tiffany",
        role_label="AI Front Desk Agent",
        headshot_url="/personas/tiffany.png",
        preview_url="/personas/tiffany.mp3",
        accent_color="#F472B6",
        description="Warm, upbeat, and conversational — caller-friendly with natural pacing.",
    ),
)

_PERSONAS_BY_SLUG: dict[str, ReceptionistPersona] = {p.slug: p for p in _PERSONAS}


def list_personas() -> list[ReceptionistPersona]:
    """Stable-ordered list of all receptionist personas."""
    return list(_PERSONAS)


def get_persona(slug: str | None) -> ReceptionistPersona:
    """Resolve a persona slug, falling back to the default on unknown / empty."""
    key = (slug or "").strip().lower()
    if key in _PERSONAS_BY_SLUG:
        return _PERSONAS_BY_SLUG[key]
    return _PERSONAS_BY_SLUG[DEFAULT_PERSONA_SLUG]


def is_valid_slug(slug: str | None) -> bool:
    """Return True iff `slug` corresponds to a known persona."""
    return (slug or "").strip().lower() in _PERSONAS_BY_SLUG


__all__ = [
    "ReceptionistPersona",
    "DEFAULT_PERSONA_SLUG",
    "list_personas",
    "get_persona",
    "is_valid_slug",
]
