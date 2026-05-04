"""Container/runtime launcher for the orchestrator.

Uses the platform-provided PORT when present and falls back to the
package default for local Docker and direct developer runs.
"""

from __future__ import annotations

import os


def resolve_port(default: int = 8000) -> int:
    """Resolve a valid TCP port from the environment."""
    raw = (os.getenv("PORT") or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def _resolve_forwarded_allow_ips() -> str:
    """Resolve the X-Forwarded-* trust list.

    security-reviewer R-W5-005 — without an explicit allowlist, uvicorn
    accepts X-Forwarded-Host / X-Forwarded-Proto from ANY upstream and
    `request.url` reflects the spoofed value. The Twilio Trust Hub
    status-callback rebuilds the URL from `request.url` to compute its
    HMAC: an attacker who can inject these headers can manipulate the
    URL used in HMAC validation.

    Default is `*` only when running locally without an upstream proxy
    (FORWARDED_ALLOW_IPS unset AND ENVIRONMENT==dev). Production should
    set FORWARDED_ALLOW_IPS to the platform's egress range — for Railway,
    that is set per-service via Railway env vars.
    """
    explicit = (os.getenv("FORWARDED_ALLOW_IPS") or "").strip()
    if explicit:
        return explicit
    env = (os.getenv("ENVIRONMENT") or "dev").strip().lower()
    if env == "dev":
        return "*"
    # Fail closed in production — only loopback by default if env didn't set
    return "127.0.0.1"


def main() -> None:
    import uvicorn

    uvicorn.run(
        "aspire_orchestrator.server:app",
        host="0.0.0.0",
        port=resolve_port(),
        proxy_headers=True,
        forwarded_allow_ips=_resolve_forwarded_allow_ips(),
    )


if __name__ == "__main__":
    main()
