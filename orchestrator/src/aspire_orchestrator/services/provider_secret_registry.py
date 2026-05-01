from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def _registry_path() -> Path:
    package_path = Path(__file__).resolve().parents[1] / "config" / "provider_secret_registry.json"
    repo_path = Path(__file__).resolve().parents[4] / "config" / "provider_secret_registry.json"
    for candidate in (package_path, repo_path):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "provider_secret_registry.json not found. "
        f"Tried {package_path} and {repo_path}."
    )


@lru_cache(maxsize=1)
def get_provider_secret_registry() -> tuple[dict[str, Any], ...]:
    data = json.loads(_registry_path().read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("provider_secret_registry.json must contain a top-level list")

    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("provider_secret_registry.json entries must be objects")
        provider = str(raw.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError("provider_secret_registry.json entries require a provider id")
        if provider in seen:
            raise ValueError(f"Duplicate provider id in registry: {provider}")
        seen.add(provider)
        normalized = dict(raw)
        normalized["provider"] = provider
        normalized["aliases"] = tuple(str(alias).strip().lower() for alias in raw.get("aliases", []) if str(alias).strip())
        normalized["env_vars"] = tuple(str(name).strip() for name in raw.get("env_vars", []) if str(name).strip())
        normalized["env_requirements"] = tuple(
            tuple(str(name).strip() for name in group if str(name).strip())
            for group in raw.get("env_requirements", [])
            if group
        )
        providers.append(normalized)
    return tuple(providers)


@lru_cache(maxsize=1)
def get_provider_secret_alias_map() -> dict[str, str]:
    aliases: dict[str, str] = {"qbo": "quickbooks"}
    for entry in get_provider_secret_registry():
        aliases[entry["provider"]] = entry["provider"]
        for alias in entry.get("aliases", ()):
            aliases[str(alias)] = entry["provider"]
    return aliases


def is_registry_provider_configured(meta: dict[str, Any], env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    requirements = meta.get("env_requirements", ())
    if requirements:
        return all(any(str(env.get(name, "")).strip() for name in group) for group in requirements)
    env_vars = meta.get("env_vars", ())
    return any(str(env.get(name, "")).strip() for name in env_vars)


def get_secret_id_for_env(meta: dict[str, Any], aspire_env: str = "dev") -> str:
    """Return the AWS Secrets Manager secret_id appropriate for the given environment.

    Pass 19 Lane B (§1.6 item 5): the registry now includes a `secret_id_by_env`
    map with keys 'dev', 'staging', 'prod'. This function resolves the correct
    path so that production services read `aspire/prod/*` and dev reads `aspire/dev/*`.

    Falls back to the top-level `secret_id` field for backward compatibility with
    registry entries that don't yet have `secret_id_by_env`.

    Args:
        meta: a registry entry dict from get_provider_secret_registry().
        aspire_env: current environment name (reads ASPIRE_ENV env var if not provided).

    Returns:
        The AWS SM secret path string, e.g. 'aspire/prod/twilio'.
    """
    env_key = aspire_env.lower().strip()
    by_env: dict[str, str] = meta.get("secret_id_by_env") or {}
    if env_key in by_env:
        return str(by_env[env_key])
    # Fallback: top-level secret_id (backward compat)
    return str(meta.get("secret_id") or "")
