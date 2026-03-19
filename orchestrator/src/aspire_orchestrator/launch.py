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


def main() -> None:
    import uvicorn

    uvicorn.run(
        "aspire_orchestrator.server:app",
        host="0.0.0.0",
        port=resolve_port(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
