#!/usr/bin/env python3
"""Post-deploy smoke test — verifies all Aspire services are reachable.

Hits health endpoints on backend orchestrator, desktop server, and admin portal.
Prints pass/fail summary and exits with code 0 (all pass) or 1 (any failure).

Usage:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --backend http://localhost:8000 --desktop http://localhost:5000 --admin http://localhost:3000

Environment variable overrides:
    SMOKE_BACKEND_URL   (default: http://localhost:8000)
    SMOKE_DESKTOP_URL   (default: http://localhost:5000)
    SMOKE_ADMIN_URL     (default: http://localhost:3000)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Sequence

import httpx

_TIMEOUT: float = 10.0


@dataclass
class ProbeResult:
    """Result of a single health probe."""

    service: str
    url: str
    passed: bool
    status_code: int | None
    latency_ms: float
    error: str | None


def probe(service: str, url: str) -> ProbeResult:
    """Send GET to a health URL and return a ProbeResult."""
    start = time.monotonic()
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        latency_ms = (time.monotonic() - start) * 1000
        passed = 200 <= resp.status_code < 400
        return ProbeResult(
            service=service,
            url=url,
            passed=passed,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            error=None if passed else f"HTTP {resp.status_code}",
        )
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            service=service, url=url, passed=False,
            status_code=None, latency_ms=latency_ms, error="timeout",
        )
    except httpx.ConnectError as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            service=service, url=url, passed=False,
            status_code=None, latency_ms=latency_ms, error=f"connection refused: {exc}",
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            service=service, url=url, passed=False,
            status_code=None, latency_ms=latency_ms, error=str(exc),
        )


def print_results(results: Sequence[ProbeResult]) -> None:
    """Print a formatted summary table."""
    print()
    print("=" * 72)
    print("  ASPIRE POST-DEPLOY SMOKE TEST")
    print("=" * 72)
    print(f"  {'Service':<20} {'Status':<8} {'Latency':<12} {'URL'}")
    print("-" * 72)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        latency = f"{r.latency_ms:.0f}ms"
        detail = r.url
        if r.error:
            detail = f"{r.url}  [{r.error}]"
        print(f"  {r.service:<20} {status:<8} {latency:<12} {detail}")
    print("-" * 72)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    verdict = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"  Result: {passed}/{total} passed — {verdict}")
    print("=" * 72)
    print()


def main(argv: Sequence[str] | None = None) -> int:
    """Run smoke tests against all services. Returns 0 on success, 1 on failure."""
    parser = argparse.ArgumentParser(description="Aspire post-deploy smoke test")
    parser.add_argument(
        "--backend",
        default=os.environ.get("SMOKE_BACKEND_URL", "http://localhost:8000"),
        help="Backend orchestrator base URL (default: $SMOKE_BACKEND_URL or http://localhost:8000)",
    )
    parser.add_argument(
        "--desktop",
        default=os.environ.get("SMOKE_DESKTOP_URL", "http://localhost:5000"),
        help="Desktop server base URL (default: $SMOKE_DESKTOP_URL or http://localhost:5000)",
    )
    parser.add_argument(
        "--admin",
        default=os.environ.get("SMOKE_ADMIN_URL", "http://localhost:3000"),
        help="Admin portal base URL (default: $SMOKE_ADMIN_URL or http://localhost:3000)",
    )
    args = parser.parse_args(argv)

    probes: list[tuple[str, str]] = [
        ("Backend /healthz", f"{args.backend.rstrip('/')}/healthz"),
        ("Backend /readyz", f"{args.backend.rstrip('/')}/readyz"),
        ("Desktop /api/health", f"{args.desktop.rstrip('/')}/api/health"),
        ("Admin Portal", f"{args.admin.rstrip('/')}"),
    ]

    results = [probe(name, url) for name, url in probes]
    print_results(results)

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
