"""Tests for browser Voice SDK token identity and TwiML webhook helpers."""

from __future__ import annotations

from aspire_orchestrator.services.twilio_voice import (
    build_identity,
    parse_identity,
    twilio_signature_url_candidates,
)


def test_voice_identity_restores_uuid_suite_id_for_supabase_lookup() -> None:
    suite_id = "085b44ec-df39-42c9-9fe9-71dae2d9d657"
    user_id = "7abf7ffc-d571-42f8-94e0-b6e973ea7244"

    identity = build_identity(suite_id=suite_id, user_id=user_id)

    parsed = parse_identity(identity)
    assert parsed["suite_id"] == suite_id
    assert parsed["user_id"] == user_id


def test_parse_identity_strips_twilio_client_prefix() -> None:
    """Twilio's voice webhook posts From=client:aspire-<suite>-<user>.

    Without the strip the route returns "Sorry, this call could not be
    completed" TwiML and the user hears the automated error message
    instead of being connected.
    """
    suite_id = "94b89098-c4bf-4419-a154-e18d9d53f993"
    user_id = "94b89098-c4bf-4419-a154-e18d9d53f993"
    bare = build_identity(suite_id=suite_id, user_id=user_id)
    wrapped = f"client:{bare}"

    assert parse_identity(wrapped) == {
        "suite_id": suite_id,
        "user_id": user_id,
    }
    # Bare form (used internally during token mint) still works.
    assert parse_identity(bare) == {
        "suite_id": suite_id,
        "user_id": user_id,
    }


def test_parse_identity_rejects_foreign_identities() -> None:
    """Identities not minted by build_identity must not be parsed.

    Includes both raw foreign identities and `client:` -prefixed ones.
    """
    for foreign in ("client:foreign-identity", "foreign-identity", ""):
        assert parse_identity(foreign) == {"suite_id": None, "user_id": None}


def test_twilio_signature_url_candidates_include_forwarded_public_url() -> None:
    assert twilio_signature_url_candidates(
        received_url="http://internal-service/v1/twilio/voice/twiml?CallSid=CA123",
        forwarded_proto="https",
        forwarded_host="ava-brain-production.up.railway.app",
        host="internal-service",
    ) == [
        "http://internal-service/v1/twilio/voice/twiml?CallSid=CA123",
        "https://ava-brain-production.up.railway.app/v1/twilio/voice/twiml?CallSid=CA123",
    ]
