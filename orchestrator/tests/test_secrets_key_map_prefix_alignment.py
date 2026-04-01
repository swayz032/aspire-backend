"""Tests for secrets KEY_MAP prefix alignment (Wave 1A).

Verifies that GROUP_KEY_MAP and KEY_MAP env vars get bridged to ASPIRE_-prefixed
names so Pydantic Settings (env_prefix="ASPIRE_") can find them.

This is the root cause fix for Ava giving generic "Done" responses (F1).
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import patch


class TestSettingsPrefixAlignment:
    """Verify _align_settings_prefix() bridges raw env vars to ASPIRE_ prefix."""

    def test_openai_key_bridged(self):
        """OPENAI_API_KEY → ASPIRE_OPENAI_API_KEY for Settings.openai_api_key."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        env_patch = {
            "OPENAI_API_KEY": "sk-test-openai-key-12345",
        }
        # Clear the ASPIRE_ version first to ensure bridging works
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            _align_settings_prefix()
            assert os.environ.get("ASPIRE_OPENAI_API_KEY") == "sk-test-openai-key-12345"

    def test_twilio_keys_bridged(self):
        """TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN → ASPIRE_ versions."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        env_patch = {
            "TWILIO_ACCOUNT_SID": "AC-test-sid",
            "TWILIO_AUTH_TOKEN": "test-auth-token",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("ASPIRE_TWILIO_ACCOUNT_SID", None)
            os.environ.pop("ASPIRE_TWILIO_AUTH_TOKEN", None)
            _align_settings_prefix()
            assert os.environ.get("ASPIRE_TWILIO_ACCOUNT_SID") == "AC-test-sid"
            assert os.environ.get("ASPIRE_TWILIO_AUTH_TOKEN") == "test-auth-token"

    def test_provider_keys_bridged(self):
        """ElevenLabs, Deepgram, Zoom, PandaDoc, Anam keys bridged."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        env_patch = {
            "ELEVENLABS_API_KEY": "el-test-key",
            "DEEPGRAM_API_KEY": "dg-test-key",
            "ZOOM_API_KEY": "zk-test-key",
            "ZOOM_API_SECRET": "zk-test-secret",
            "PANDADOC_API_KEY": "pd-test-key",
            "ANAM_API_KEY": "anam-test-key",
        }
        aspire_keys = [
            "ASPIRE_ELEVENLABS_API_KEY",
            "ASPIRE_DEEPGRAM_API_KEY",
            "ASPIRE_ZOOM_API_KEY",
            "ASPIRE_ZOOM_API_SECRET",
            "ASPIRE_PANDADOC_API_KEY",
            "ASPIRE_ANAM_API_KEY",
        ]
        with patch.dict(os.environ, env_patch, clear=False):
            for k in aspire_keys:
                os.environ.pop(k, None)
            _align_settings_prefix()
            assert os.environ.get("ASPIRE_ELEVENLABS_API_KEY") == "el-test-key"
            assert os.environ.get("ASPIRE_DEEPGRAM_API_KEY") == "dg-test-key"
            assert os.environ.get("ASPIRE_ZOOM_API_KEY") == "zk-test-key"
            assert os.environ.get("ASPIRE_ZOOM_API_SECRET") == "zk-test-secret"
            assert os.environ.get("ASPIRE_PANDADOC_API_KEY") == "pd-test-key"
            assert os.environ.get("ASPIRE_ANAM_API_KEY") == "anam-test-key"

    def test_no_overwrite_existing_aspire_prefix(self):
        """If ASPIRE_ version already set, don't overwrite it."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        env_patch = {
            "OPENAI_API_KEY": "raw-key",
            "ASPIRE_OPENAI_API_KEY": "already-set-key",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            _align_settings_prefix()
            assert os.environ.get("ASPIRE_OPENAI_API_KEY") == "already-set-key"

    def test_empty_raw_key_not_bridged(self):
        """Empty raw env var should not create empty ASPIRE_ version."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            _align_settings_prefix()
            # Should NOT be set because raw value is falsy
            assert os.environ.get("ASPIRE_OPENAI_API_KEY") is None

    def test_missing_raw_key_not_bridged(self):
        """Missing raw env var should not create ASPIRE_ version."""
        from aspire_orchestrator.config.secrets import _align_settings_prefix

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            _align_settings_prefix()
            assert os.environ.get("ASPIRE_OPENAI_API_KEY") is None


class TestVerifySettingsCoverage:
    """Verify that verify_settings_coverage() detects empty fields."""

    def _make_mock_settings(self, **overrides):
        """Create a mock Settings object with all fields populated by default."""
        from unittest.mock import MagicMock
        mock = MagicMock()
        defaults = {
            "openai_api_key": "sk-key",
            "supabase_url": "https://test.supabase.co",
            "supabase_service_role_key": "test-key",
            "stripe_api_key": "sk_test",
            "elevenlabs_api_key": "el-key",
            "deepgram_api_key": "dg-key",
            "zoom_api_key": "zk-key",
            "zoom_api_secret": "zk-sec",
            "twilio_account_sid": "AC-sid",
            "twilio_auth_token": "tw-tok",
            "pandadoc_api_key": "pd-key",
            "token_signing_key": "sign-key",
        }
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(mock, k, v)
        return mock

    def test_reports_empty_critical_fields(self):
        """Empty ASPIRE_OPENAI_API_KEY should be reported as missing field."""
        from aspire_orchestrator.config.secrets import verify_settings_coverage

        # verify_settings_coverage checks os.environ, not Settings object
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            warnings = verify_settings_coverage()
            assert "openai_api_key" in warnings

    def test_all_populated_no_warnings(self):
        """All fields populated → zero warnings."""
        from aspire_orchestrator.config.secrets import verify_settings_coverage

        full_env = {
            "ASPIRE_OPENAI_API_KEY": "sk-key",
            "ASPIRE_ELEVENLABS_API_KEY": "el-key",
            "ASPIRE_DEEPGRAM_API_KEY": "dg-key",
            "ASPIRE_ZOOM_API_KEY": "zk-key",
            "ASPIRE_TWILIO_ACCOUNT_SID": "AC-sid",
            "ASPIRE_PANDADOC_API_KEY": "pd-key",
            "STRIPE_SECRET_KEY": "sk_test_stripe",
            "ASPIRE_SUPABASE_SERVICE_ROLE_KEY": "eyJ-supa",
            "TOKEN_SIGNING_SECRET": "sign-secret-key",
        }
        with patch.dict(os.environ, full_env, clear=False):
            warnings = verify_settings_coverage()
            assert len(warnings) == 0


class TestSettingsPrefixMapCompleteness:
    """Verify _SETTINGS_PREFIX_MAP covers all Settings provider fields."""

    def test_all_provider_fields_have_prefix_mapping(self):
        """Every provider key in Settings should have a prefix mapping entry."""
        from aspire_orchestrator.config.secrets import _SETTINGS_PREFIX_MAP

        # These are the Settings fields that have raw env var counterparts
        # that need ASPIRE_ prefix bridging
        expected_aspire_keys = {
            "ASPIRE_OPENAI_API_KEY",
            "ASPIRE_TWILIO_ACCOUNT_SID",
            "ASPIRE_TWILIO_AUTH_TOKEN",
            "ASPIRE_ELEVENLABS_API_KEY",
            "ASPIRE_DEEPGRAM_API_KEY",
            "ASPIRE_ZOOM_SDK_KEY",
            "ASPIRE_ZOOM_SDK_SECRET",
            "ASPIRE_ZOOM_API_KEY",
            "ASPIRE_ZOOM_API_SECRET",
            "ASPIRE_PANDADOC_API_KEY",
            "ASPIRE_ANAM_API_KEY",
            "ASPIRE_TOKEN_SIGNING_KEY",
            "ASPIRE_STRIPE_WEBHOOK_SECRET",
        }
        actual_keys = set(_SETTINGS_PREFIX_MAP.keys())
        assert expected_aspire_keys == actual_keys, (
            f"Missing: {expected_aspire_keys - actual_keys}, "
            f"Extra: {actual_keys - expected_aspire_keys}"
        )


class TestLoadSecretsCallsAlignment:
    """Verify load_secrets() calls _align_settings_prefix() in all code paths."""

    def test_local_dev_mode_calls_alignment(self):
        """Local dev without AWS creds should still call prefix alignment."""
        from aspire_orchestrator.config.secrets import load_secrets, _last_fetch
        import aspire_orchestrator.config.secrets as secrets_mod

        original_fetch = secrets_mod._last_fetch

        env_patch = {
            "ASPIRE_ENV": "local",
            "OPENAI_API_KEY": "sk-test-from-env",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            # Reset cache so load_secrets actually runs
            secrets_mod._last_fetch = 0
            try:
                load_secrets()
                assert os.environ.get("ASPIRE_OPENAI_API_KEY") == "sk-test-from-env"
            finally:
                secrets_mod._last_fetch = original_fetch
