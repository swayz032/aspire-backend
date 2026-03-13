#!/usr/bin/env python3
"""Shared runtime configuration for local n8n helper scripts.

These scripts must never embed real credentials in source control.
Everything sensitive is loaded from the environment at runtime.
"""

from __future__ import annotations

import os


def env_required(name: str, *, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return str(value).strip()


def env_optional(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def get_n8n_base_url() -> str:
    return env_optional("ASPIRE_N8N_URL", env_optional("N8N_BASE_URL", "http://localhost:5678")).rstrip("/")


def get_gateway_url() -> str:
    return env_optional("ASPIRE_GATEWAY_URL", env_optional("GATEWAY_URL", "http://localhost:5000")).rstrip("/")


def get_n8n_api_key() -> str:
    return env_required("N8N_API_KEY")


def get_n8n_admin_email() -> str:
    return env_optional("N8N_ADMIN_EMAIL", "admin@aspireos.app")


def get_n8n_admin_password() -> str:
    return env_required("N8N_ADMIN_PASSWORD")


_WEBHOOK_SECRET_ENV = {
    "intake": ("N8N_INTAKE_WEBHOOK_SECRET", "ASPIRE_N8N_HMAC_SECRET"),
    "eli": ("N8N_ELI_WEBHOOK_SECRET",),
    "sarah": ("N8N_SARAH_WEBHOOK_SECRET",),
    "nora": ("N8N_NORA_WEBHOOK_SECRET",),
}


def get_webhook_secret(name: str) -> str:
    candidates = _WEBHOOK_SECRET_ENV.get(name, ())
    for candidate in candidates:
        value = env_optional(candidate)
        if value:
            return value
    missing = ", ".join(candidates) if candidates else name
    raise RuntimeError(f"Missing webhook secret environment variable for {name}: {missing}")
