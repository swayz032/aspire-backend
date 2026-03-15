"""Soak test / load test script for Aspire Orchestrator.

Runs HTTP load against key endpoints and validates SLO targets:
  - p50 < 500ms
  - p95 < 2s
  - p99 < 5s
  - Error rate < 1%

Usage:
  # Quick smoke (30s, 2 concurrent)
  python scripts/load_test.py --duration 30 --concurrency 2

  # Full 24h soak test
  python scripts/load_test.py --duration 86400 --concurrency 5

  # Custom target
  python scripts/load_test.py --base-url http://localhost:8000 --duration 3600

Requires: pip install aiohttp  (already in dev dependencies)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is required. Install with: pip install aiohttp", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("soak_test")

# ---------------------------------------------------------------------------
# SLO thresholds
# ---------------------------------------------------------------------------
SLO_P50_MS = 500
SLO_P95_MS = 2_000
SLO_P99_MS = 5_000
SLO_ERROR_RATE_PCT = 1.0

# ---------------------------------------------------------------------------
# Endpoint definitions
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    """HTTP endpoint to test."""
    method: str
    path: str
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    expected_status: set[int] = field(default_factory=lambda: {200})
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"{self.method} {self.path}"


def build_endpoints(suite_id: str, office_id: str, actor_id: str) -> list[Endpoint]:
    """Build the list of endpoints to test."""
    auth_headers = {
        "X-Suite-Id": suite_id,
        "X-Office-Id": office_id,
        "X-Actor-Id": actor_id,
    }

    return [
        # Health probes (no auth)
        Endpoint(method="GET", path="/healthz", name="healthz"),
        Endpoint(method="GET", path="/livez", name="livez"),
        Endpoint(
            method="GET",
            path="/readyz",
            name="readyz",
            expected_status={200, 503},  # 503 if dependencies down
        ),

        # Metrics (no auth)
        Endpoint(method="GET", path="/metrics", name="metrics"),

        # Intent classification (auth required)
        Endpoint(
            method="POST",
            path="/v1/intents/classify",
            body={
                "utterance": "What is my schedule for today?",
                "suite_id": suite_id,
                "office_id": office_id,
                "actor_id": actor_id,
                "channel": "internal_frontend",
            },
            headers=auth_headers,
            name="intents/classify",
        ),

        # Receipt query (auth required)
        Endpoint(
            method="GET",
            path="/v1/receipts?limit=5",
            headers=auth_headers,
            name="receipts",
        ),

        # Policy evaluate (auth required)
        Endpoint(
            method="POST",
            path="/v1/policy/evaluate",
            body={
                "action_type": "calendar.read",
                "suite_id": suite_id,
                "actor_id": actor_id,
            },
            headers=auth_headers,
            name="policy/evaluate",
        ),

        # Registry capabilities (auth required)
        Endpoint(
            method="GET",
            path="/v1/registry/capabilities",
            headers=auth_headers,
            name="registry/capabilities",
        ),
    ]


# ---------------------------------------------------------------------------
# Stats collection
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    """Mutable statistics collector."""
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    total: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, latency_ms: float, status: int, is_error: bool) -> None:
        async with self._lock:
            self.total += 1
            self.latencies_ms.append(latency_ms)
            self.status_codes[status] = self.status_codes.get(status, 0) + 1
            if is_error:
                self.errors += 1

    def report(self) -> dict[str, Any]:
        if not self.latencies_ms:
            return {"total": 0, "errors": 0, "error_rate_pct": 0.0}

        sorted_lat = sorted(self.latencies_ms)
        n = len(sorted_lat)

        def percentile(pct: float) -> float:
            idx = int(pct / 100 * n)
            return sorted_lat[min(idx, n - 1)]

        error_rate = (self.errors / self.total * 100) if self.total > 0 else 0.0

        return {
            "total": self.total,
            "errors": self.errors,
            "error_rate_pct": round(error_rate, 3),
            "p50_ms": round(percentile(50), 1),
            "p95_ms": round(percentile(95), 1),
            "p99_ms": round(percentile(99), 1),
            "min_ms": round(sorted_lat[0], 1),
            "max_ms": round(sorted_lat[-1], 1),
            "mean_ms": round(statistics.mean(sorted_lat), 1),
            "status_codes": dict(sorted(self.status_codes.items())),
        }


# ---------------------------------------------------------------------------
# Load runner
# ---------------------------------------------------------------------------

async def run_request(
    session: aiohttp.ClientSession,
    endpoint: Endpoint,
    base_url: str,
    stats: Stats,
) -> None:
    """Execute a single request and record stats."""
    url = f"{base_url}{endpoint.path}"
    start = time.monotonic()
    status = 0
    is_error = False

    try:
        kwargs: dict[str, Any] = {"headers": endpoint.headers}
        if endpoint.body is not None:
            kwargs["json"] = endpoint.body

        async with session.request(endpoint.method, url, **kwargs) as resp:
            status = resp.status
            await resp.read()  # consume body
            is_error = status not in endpoint.expected_status
    except Exception as exc:
        status = 0
        is_error = True
        logger.debug("Request failed: %s %s -> %s", endpoint.method, endpoint.path, exc)

    elapsed_ms = (time.monotonic() - start) * 1000
    await stats.record(elapsed_ms, status, is_error)


async def worker(
    session: aiohttp.ClientSession,
    endpoints: list[Endpoint],
    base_url: str,
    per_endpoint_stats: dict[str, Stats],
    global_stats: Stats,
    stop_event: asyncio.Event,
    rps_target: float,
) -> None:
    """Continuously send requests until stop_event is set."""
    delay = 1.0 / rps_target if rps_target > 0 else 0.1
    ep_idx = 0

    while not stop_event.is_set():
        ep = endpoints[ep_idx % len(endpoints)]
        ep_idx += 1

        await run_request(session, ep, base_url, per_endpoint_stats[ep.name])
        await run_request(session, ep, base_url, global_stats)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            pass


async def run_soak_test(
    base_url: str,
    duration_s: int,
    concurrency: int,
    rps_per_worker: float,
    suite_id: str,
    office_id: str,
    actor_id: str,
    report_interval_s: int = 60,
) -> bool:
    """Run the soak test. Returns True if all SLOs pass."""
    endpoints = build_endpoints(suite_id, office_id, actor_id)
    per_endpoint_stats = {ep.name: Stats() for ep in endpoints}
    global_stats = Stats()
    stop_event = asyncio.Event()

    logger.info(
        "Starting soak test: base_url=%s duration=%ds concurrency=%d rps_per_worker=%.1f",
        base_url, duration_s, concurrency, rps_per_worker,
    )
    logger.info("Endpoints: %s", [ep.name for ep in endpoints])
    logger.info(
        "SLO targets: p50<%dms p95<%dms p99<%dms error_rate<%.1f%%",
        SLO_P50_MS, SLO_P95_MS, SLO_P99_MS, SLO_ERROR_RATE_PCT,
    )

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=concurrency * 2, limit_per_host=concurrency * 2)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # Spawn workers
        tasks = [
            asyncio.create_task(
                worker(session, endpoints, base_url, per_endpoint_stats, global_stats, stop_event, rps_per_worker)
            )
            for _ in range(concurrency)
        ]

        # Periodic reporting
        start_time = time.monotonic()
        elapsed = 0.0

        while elapsed < duration_s:
            await asyncio.sleep(min(report_interval_s, duration_s - elapsed))
            elapsed = time.monotonic() - start_time

            report = global_stats.report()
            logger.info(
                "Progress [%.0fs/%.0fs]: total=%d errors=%d err_rate=%.2f%% p50=%.0fms p95=%.0fms p99=%.0fms",
                elapsed, duration_s,
                report["total"], report["errors"], report["error_rate_pct"],
                report.get("p50_ms", 0), report.get("p95_ms", 0), report.get("p99_ms", 0),
            )

        # Stop workers
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Final report
    logger.info("=" * 72)
    logger.info("SOAK TEST COMPLETE — Final Report")
    logger.info("=" * 72)

    global_report = global_stats.report()
    _print_report("GLOBAL", global_report)

    for ep_name, ep_stats in per_endpoint_stats.items():
        ep_report = ep_stats.report()
        if ep_report["total"] > 0:
            _print_report(ep_name, ep_report)

    # SLO evaluation
    slo_pass = _evaluate_slos(global_report)
    return slo_pass


def _print_report(name: str, report: dict[str, Any]) -> None:
    logger.info("--- %s ---", name)
    logger.info(
        "  Requests: %d | Errors: %d (%.2f%%)",
        report["total"], report["errors"], report["error_rate_pct"],
    )
    if report["total"] > 0:
        logger.info(
            "  Latency: p50=%.0fms p95=%.0fms p99=%.0fms min=%.0fms max=%.0fms mean=%.0fms",
            report.get("p50_ms", 0), report.get("p95_ms", 0), report.get("p99_ms", 0),
            report.get("min_ms", 0), report.get("max_ms", 0), report.get("mean_ms", 0),
        )
        logger.info("  Status codes: %s", report.get("status_codes", {}))


def _evaluate_slos(report: dict[str, Any]) -> bool:
    """Check SLO targets against collected metrics."""
    if report["total"] == 0:
        logger.error("SLO FAIL: No requests completed")
        return False

    violations: list[str] = []

    p50 = report.get("p50_ms", 0)
    p95 = report.get("p95_ms", 0)
    p99 = report.get("p99_ms", 0)
    error_rate = report.get("error_rate_pct", 0)

    if p50 > SLO_P50_MS:
        violations.append(f"p50={p50:.0f}ms > {SLO_P50_MS}ms")
    if p95 > SLO_P95_MS:
        violations.append(f"p95={p95:.0f}ms > {SLO_P95_MS}ms")
    if p99 > SLO_P99_MS:
        violations.append(f"p99={p99:.0f}ms > {SLO_P99_MS}ms")
    if error_rate > SLO_ERROR_RATE_PCT:
        violations.append(f"error_rate={error_rate:.2f}% > {SLO_ERROR_RATE_PCT}%")

    if violations:
        logger.error("SLO VIOLATIONS: %s", " | ".join(violations))
        return False

    logger.info("ALL SLOs PASS: p50=%.0fms p95=%.0fms p99=%.0fms error_rate=%.2f%%", p50, p95, p99, error_rate)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Aspire Orchestrator Soak Test")
    parser.add_argument("--base-url", default=os.getenv("SOAK_TEST_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--duration", type=int, default=int(os.getenv("SOAK_TEST_DURATION", "300")),
                        help="Test duration in seconds (default: 300, use 86400 for 24h soak)")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("SOAK_TEST_CONCURRENCY", "3")),
                        help="Number of concurrent workers")
    parser.add_argument("--rps", type=float, default=float(os.getenv("SOAK_TEST_RPS", "2.0")),
                        help="Target requests per second per worker")
    parser.add_argument("--report-interval", type=int, default=60,
                        help="Seconds between progress reports")
    parser.add_argument("--suite-id", default=os.getenv("SOAK_TEST_SUITE_ID", "soak-test-suite"))
    parser.add_argument("--office-id", default=os.getenv("SOAK_TEST_OFFICE_ID", "soak-test-office"))
    parser.add_argument("--actor-id", default=os.getenv("SOAK_TEST_ACTOR_ID", "soak-test-actor"))
    args = parser.parse_args()

    success = asyncio.run(
        run_soak_test(
            base_url=args.base_url,
            duration_s=args.duration,
            concurrency=args.concurrency,
            rps_per_worker=args.rps,
            suite_id=args.suite_id,
            office_id=args.office_id,
            actor_id=args.actor_id,
            report_interval_s=args.report_interval,
        )
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
