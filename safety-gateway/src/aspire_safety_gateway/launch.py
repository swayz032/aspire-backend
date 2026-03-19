"""Container/runtime launcher for the safety gateway."""

from __future__ import annotations

import os


def resolve_port(default: int = 8787) -> int:
    raw = (os.getenv("PORT") or os.getenv("ASPIRE_SAFETY_GATEWAY_PORT") or "").strip()
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
        "aspire_safety_gateway.app:app",
        host="0.0.0.0",
        port=resolve_port(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
