"""Tests for the receptionist persona feature (Sarah / Tiffany picker).

Covers:
  - Persona registry (services.receptionist_personas)
  - GET  /v1/front-desk/personas — Green tier, no auth
  - PATCH /v1/front-desk/config { receptionist_persona } — versioned write +
    EL re-attach + receipt fan-out
  - Validation: unknown persona -> 422
  - Idempotency: same-slug PATCH -> NO swap call
  - Pre-purchase: persona change with no number -> deferred receipt
  - EL failure: failed receipt, no rollback of front_desk_configs insert
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.front_desk import router as front_desk_router
from aspire_orchestrator.services import receptionist_personas as personas_mod

_app = FastAPI()
_app.include_router(front_desk_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"
_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


def _mint_token(scope: str) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="front_desk",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def _current_config(persona: str = "sarah", version_no: int = 3) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "version_no": version_no,
        "is_current": True,
        "after_hours_mode": "take_message",
        "busy_mode": "take_message",
        "public_number_mode": "ASPIRE_NUMBER",
        "catch_mode": "APP_AND_PHONE_SIMUL_RING",
        "greeting_name_override": "",
        "pronunciation_override": "",
        "receptionist_persona": persona,
    }


# ---------------------------------------------------------------------------
# Registry — pure module tests (no FastAPI required)
# ---------------------------------------------------------------------------


def test_registry_default_is_sarah():
    """Default slug is 'sarah' so brand-new tenants get the canonical persona."""
    assert personas_mod.DEFAULT_PERSONA_SLUG == "sarah"
    sarah = personas_mod.get_persona("sarah")
    assert sarah.slug == "sarah"
    assert sarah.display_name == "Sarah"
    assert sarah.agent_id.startswith("agent_")
    assert sarah.preview_url.endswith(".mp3")


def test_registry_tiffany_distinct_from_sarah():
    """Tiffany must have a different agent_id AND voice_id from Sarah."""
    sarah = personas_mod.get_persona("sarah")
    tiffany = personas_mod.get_persona("tiffany")
    assert sarah.agent_id != tiffany.agent_id, "Sarah and Tiffany must hit different EL agents"
    assert sarah.voice_id != tiffany.voice_id, "Sarah and Tiffany must use different voices"
    assert sarah.accent_color != tiffany.accent_color, "UI accent must be visually distinct"


def test_registry_unknown_slug_falls_back_to_default():
    """Unknown slug -> get_persona returns the default (defensive)."""
    p = personas_mod.get_persona("napoleon")
    assert p.slug == personas_mod.DEFAULT_PERSONA_SLUG


def test_registry_is_valid_slug():
    assert personas_mod.is_valid_slug("sarah") is True
    assert personas_mod.is_valid_slug("Tiffany") is True  # case-insensitive
    assert personas_mod.is_valid_slug("napoleon") is False
    assert personas_mod.is_valid_slug(None) is False
    assert personas_mod.is_valid_slug("") is False


def test_registry_list_personas_stable_order():
    """list_personas() must return Sarah first (default) then Tiffany."""
    items = personas_mod.list_personas()
    assert len(items) >= 2
    assert items[0].slug == "sarah"
    assert items[1].slug == "tiffany"


# ---------------------------------------------------------------------------
# GET /personas — Green tier, no auth required
# ---------------------------------------------------------------------------


def test_get_personas_no_auth():
    """GET /personas must succeed without scope headers or capability token."""
    resp = _client.get("/v1/front-desk/personas")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["default_persona"] == "sarah"
    slugs = [p["slug"] for p in body["personas"]]
    assert "sarah" in slugs
    assert "tiffany" in slugs
    # Required fields present on every persona
    for p in body["personas"]:
        for key in (
            "slug", "agent_id", "voice_id", "display_name", "role_label",
            "headshot_url", "preview_url", "accent_color", "description",
        ):
            assert key in p, f"persona '{p.get('slug')}' missing {key}"


# ---------------------------------------------------------------------------
# PATCH /config — receptionist_persona validation + versioning
# ---------------------------------------------------------------------------


def test_patch_unknown_persona_rejected_422():
    """Unknown persona slug returns 422 with UNKNOWN_PERSONA error code."""
    cap_token = _mint_token("front_desk:config_save")
    resp = _client.patch(
        "/v1/front-desk/config",
        headers=_SCOPE_HEADERS,
        json={"receptionist_persona": "napoleon", "capability_token": cap_token},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"] == "UNKNOWN_PERSONA"
    assert "napoleon" in body["detail"]["message"]


def test_patch_no_token_rejected_401():
    """PATCH without capability token returns 401 even with a valid persona."""
    resp = _client.patch(
        "/v1/front-desk/config",
        headers=_SCOPE_HEADERS,
        json={"receptionist_persona": "tiffany"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "MISSING_CAPABILITY_TOKEN"


def test_patch_persona_change_versions_and_swaps():
    """Changing persona inserts new version + calls EL attach + cuts receipts."""
    cap_token = _mint_token("front_desk:config_save")
    current = _current_config(persona="sarah", version_no=4)
    inserted_row = {**current, "version_no": 5, "receptionist_persona": "tiffany"}
    phone_row = {
        "id": str(uuid.uuid4()),
        "elevenlabs_phone_number_id": "pn_abc123def456",
        "attached_to_agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
    }

    with (
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_select",
            new=AsyncMock(side_effect=[[current], [phone_row]]),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_insert",
            new=AsyncMock(return_value=inserted_row),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_update",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.attach_to_agent",
            new=AsyncMock(return_value=None),
        ) as mock_attach,
        patch(
            "aspire_orchestrator.routes.front_desk.receipt_store.store_receipts",
        ) as mock_store,
    ):
        resp = _client.patch(
            "/v1/front-desk/config",
            headers=_SCOPE_HEADERS,
            json={"receptionist_persona": "tiffany", "capability_token": cap_token},
        )

    assert resp.status_code == 200, resp.text
    # EL attach called with Tiffany's agent_id
    mock_attach.assert_awaited_once()
    _, kwargs = mock_attach.call_args
    assert kwargs.get("agent_id") == personas_mod.get_persona("tiffany").agent_id
    # At least 2 receipts cut: receptionist_persona_changed + front_desk_config_save
    assert mock_store.call_count >= 2
    receipt_types = {
        r["receipt_type"]
        for call in mock_store.call_args_list
        for r in call.args[0]
    }
    assert "receptionist_persona_changed" in receipt_types
    assert "front_desk_config_save" in receipt_types


def test_patch_same_persona_no_swap():
    """Saving config without changing persona must NOT call EL attach (idempotent)."""
    cap_token = _mint_token("front_desk:config_save")
    current = _current_config(persona="sarah")
    inserted_row = {**current, "version_no": current["version_no"] + 1}

    with (
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_select",
            new=AsyncMock(return_value=[current]),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_insert",
            new=AsyncMock(return_value=inserted_row),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.attach_to_agent",
            new=AsyncMock(return_value=None),
        ) as mock_attach,
        patch(
            "aspire_orchestrator.routes.front_desk.receipt_store.store_receipts",
        ),
    ):
        resp = _client.patch(
            "/v1/front-desk/config",
            headers=_SCOPE_HEADERS,
            # Re-saving sarah while sarah is already current
            json={"receptionist_persona": "sarah", "capability_token": cap_token},
        )

    assert resp.status_code == 200
    mock_attach.assert_not_awaited()


def test_patch_persona_change_pre_purchase_deferred():
    """Persona change for an office without a phone number -> deferred receipt, no EL call."""
    cap_token = _mint_token("front_desk:config_save")
    current = _current_config(persona="sarah")
    inserted_row = {**current, "version_no": current["version_no"] + 1, "receptionist_persona": "tiffany"}

    with (
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_select",
            new=AsyncMock(side_effect=[[current], []]),  # 2nd select = no phone rows
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_insert",
            new=AsyncMock(return_value=inserted_row),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.attach_to_agent",
            new=AsyncMock(return_value=None),
        ) as mock_attach,
        patch(
            "aspire_orchestrator.routes.front_desk.receipt_store.store_receipts",
        ) as mock_store,
    ):
        resp = _client.patch(
            "/v1/front-desk/config",
            headers=_SCOPE_HEADERS,
            json={"receptionist_persona": "tiffany", "capability_token": cap_token},
        )

    assert resp.status_code == 200
    mock_attach.assert_not_awaited()
    # Persona-change receipt must have outcome='deferred_no_number'
    persona_receipts = [
        r
        for call in mock_store.call_args_list
        for r in call.args[0]
        if r["receipt_type"] == "receptionist_persona_changed"
    ]
    assert len(persona_receipts) == 1
    assert persona_receipts[0]["outcome"] == "deferred_no_number"


def test_patch_persona_change_el_failure_persists_intent():
    """EL failure on swap must NOT roll back the front_desk_configs insert.

    The DB row is the source of truth for tenant intent; EL is best-effort
    and reconciled on the next save. A 'failed' receipt is cut so the audit
    trail reflects the divergence.
    """
    from aspire_orchestrator.services.elevenlabs_phone import ElevenLabsPhoneError
    cap_token = _mint_token("front_desk:config_save")
    current = _current_config(persona="sarah")
    inserted_row = {**current, "version_no": current["version_no"] + 1, "receptionist_persona": "tiffany"}
    phone_row = {
        "id": str(uuid.uuid4()),
        "elevenlabs_phone_number_id": "pn_abc123def456",
    }

    with (
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_select",
            new=AsyncMock(side_effect=[[current], [phone_row]]),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.supabase_insert",
            new=AsyncMock(return_value=inserted_row),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.attach_to_agent",
            new=AsyncMock(side_effect=ElevenLabsPhoneError("EL_DOWN", "boom", 503)),
        ),
        patch(
            "aspire_orchestrator.routes.front_desk.receipt_store.store_receipts",
        ) as mock_store,
    ):
        resp = _client.patch(
            "/v1/front-desk/config",
            headers=_SCOPE_HEADERS,
            json={"receptionist_persona": "tiffany", "capability_token": cap_token},
        )

    # API returns success — DB persistence happened, EL is best-effort
    assert resp.status_code == 200
    persona_receipts = [
        r
        for call in mock_store.call_args_list
        for r in call.args[0]
        if r["receipt_type"] == "receptionist_persona_changed"
    ]
    assert len(persona_receipts) == 1
    assert persona_receipts[0]["outcome"] == "failed"
    assert persona_receipts[0]["reason_code"] == "EL_DOWN"
