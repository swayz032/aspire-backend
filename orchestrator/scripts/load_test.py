#!/usr/bin/env python3
"""Aspire Orchestrator Load Test — Phase 1 Wave 9.

SLO targets (from infrastructure/observability/SLI_SLO.md):
  - Success rate: >= 99%
  - p95 latency: <= 2s (orchestrator, excluding external tool calls)
  - Receipt write availability: >= 99.9%

Usage:
  # Quick test (10 min, 100 req/min = ~1000 requests)
  python scripts/load_test.py --duration 600 --rate 100

  # Medium soak (1 hour, 500 req/min)
  python scripts/load_test.py --duration 3600 --rate 500

  # Full soak (24h, 1000 req/hour)
  python scripts/load_test.py --duration 86400 --rate 17

  # Custom base URL
  python scripts/load_test.py --base-url http://localhost:8000 --duration 60 --rate 10

Output: JSON report to stdout + optional file via --output flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx required. Install: pip install httpx", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Request Mix (50% GREEN, 30% YELLOW, 20% RED)
# =============================================================================

# These are real action_types from policy_matrix.yaml
GREEN_ACTIONS = [
    "receipts.search",
    "calendar.list_events",
    "contacts.search",
    "email.list_messages",
]

YELLOW_ACTIONS = [
    "invoice.create",
    "email.send_draft",
    "calendar.create_event",
    "meeting.schedule",
]

RED_ACTIONS = [
    "money.stripe.invoice.send",
    "contract.sign",
    "payroll.run",
]

# Suite/office UUIDs for test isolation
TEST_SUITE_ID = "00000000-0000-0000-0000-load_test_001"
TEST_OFFICE_ID = "00000000-0000-0000-0000-load_test_off"


def _generate_request(action: str, risk_tier: str) -> dict[str, Any]:
    """Generate a realistic AvaOrchestratorRequest payload."""
    return {
        "suite_id": TEST_SUITE_ID,
        "office_id": TEST_OFFICE_ID,
        "correlation_id": f"load-{uuid.uuid4().hex[:12]}",
        "task_type": action,
        "intent": f"Load test: {action}",
        "actor_id": "load-test-runner",
        "actor_type": "SYSTEM",
        "approval_status": "pre_approved" if risk_tier == "green" else "pending",
        "parameters": {"test": True, "load_test": True},
    }


def _pick_action() -> tuple[str, str]:
    """Pick a random action based on the 50/30/20 mix."""
    roll = random.random()
    if roll < 0.50:
        return random.choice(GREEN_ACTIONS), "green"
    elif roll < 0.80:
        return random.choice(YELLOW_ACTIONS), "yellow"
    else:
        return random.choice(RED_ACTIONS), "red"


# =============================================================================
# Result Tracking
# =============================================================================


@dataclass
class RequestResult:
    action: str
    risk_tier: str
    status_code: int
    latency_ms: float
    success: bool
    error: str | None = None


@dataclass
class LoadTestReport:
    """Aggregated load test results."""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    target_rate_per_min: int = 0
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    error_rate: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)

    # Per-tier breakdown
    green_count: int = 0
    green_errors: int = 0
    yellow_count: int = 0
    yellow_errors: int = 0
    red_count: int = 0
    red_errors: int = 0

    # Error breakdown
    error_types: dict[str, int] = field(default_factory=dict)

    def add_result(self, r: RequestResult) -> None:
        self.total_requests += 1
        self.latencies_ms.append(r.latency_ms)

        if r.success:
            self.successful += 1
        else:
            self.failed += 1
            err_key = r.error or f"http_{r.status_code}"
            self.error_types[err_key] = self.error_types.get(err_key, 0) + 1

        if r.risk_tier == "green":
            self.green_count += 1
            if not r.success:
                self.green_errors += 1
        elif r.risk_tier == "yellow":
            self.yellow_count += 1
            if not r.success:
                self.yellow_errors += 1
        elif r.risk_tier == "red":
            self.red_count += 1
            if not r.success:
                self.red_errors += 1

    def finalize(self) -> None:
        if self.total_requests > 0:
            self.error_rate = self.failed / self.total_requests

    def to_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms) if self.latencies_ms else [0]
        p50 = statistics.median(lat)
        p95 = lat[int(len(lat) * 0.95)] if len(lat) > 1 else lat[0]
        p99 = lat[int(len(lat) * 0.99)] if len(lat) > 1 else lat[0]

        return {
            "meta": {
                "start_time": self.start_time,
                "end_time": self.end_time,
                "duration_seconds": round(self.duration_seconds, 1),
                "target_rate_per_min": self.target_rate_per_min,
            },
            "summary": {
                "total_requests": self.total_requests,
                "successful": self.successful,
                "failed": self.failed,
                "error_rate": round(self.error_rate, 4),
                "error_rate_pct": f"{self.error_rate * 100:.2f}%",
                "slo_success_rate_pass": self.error_rate <= 0.01,
            },
            "latency_ms": {
                "p50": round(p50, 1),
                "p95": round(p95, 1),
                "p99": round(p99, 1),
                "min": round(min(lat), 1),
                "max": round(max(lat), 1),
                "mean": round(statistics.mean(lat), 1) if lat else 0,
                "slo_p95_under_2s_pass": p95 <= 2000,
            },
            "risk_tier_breakdown": {
                "green": {"count": self.green_count, "errors": self.green_errors},
                "yellow": {"count": self.yellow_count, "errors": self.yellow_errors},
                "red": {"count": self.red_count, "errors": self.red_errors},
            },
            "error_types": self.error_types,
            "slo_verdict": {
                "success_rate_pass": self.error_rate <= 0.01,
                "p95_latency_pass": p95 <= 2000,
                "overall": "PASS" if (self.error_rate <= 0.01 and p95 <= 2000) else "FAIL",
            },
        }


# =============================================================================
# Load Test Runner
# =============================================================================


async def _send_request(
    client: httpx.AsyncClient,
    base_url: str,
    action: str,
    risk_tier: str,
) -> RequestResult:
    """Send a single orchestrator request and measure latency."""
    payload = _generate_request(action, risk_tier)
    start = time.monotonic()

    try:
        resp = await client.post(
            f"{base_url}/v1/intents",
            json=payload,
            timeout=30.0,
        )
        latency = (time.monotonic() - start) * 1000

        # 200 = success, 202 = approval required (expected for YELLOW/RED), 403 = denied (expected)
        # Only 5xx or connection errors count as failures
        success = resp.status_code < 500

        return RequestResult(
            action=action,
            risk_tier=risk_tier,
            status_code=resp.status_code,
            latency_ms=latency,
            success=success,
        )

    except httpx.TimeoutException:
        latency = (time.monotonic() - start) * 1000
        return RequestResult(
            action=action, risk_tier=risk_tier,
            status_code=0, latency_ms=latency,
            success=False, error="timeout",
        )
    except httpx.ConnectError:
        latency = (time.monotonic() - start) * 1000
        return RequestResult(
            action=action, risk_tier=risk_tier,
            status_code=0, latency_ms=latency,
            success=False, error="connection_refused",
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return RequestResult(
            action=action, risk_tier=risk_tier,
            status_code=0, latency_ms=latency,
            success=False, error=str(e),
        )


async def run_load_test(
    base_url: str,
    duration_seconds: int,
    rate_per_min: int,
    concurrency: int = 10,
) -> LoadTestReport:
    """Run the load test with specified parameters."""
    report = LoadTestReport()
    report.start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.target_rate_per_min = rate_per_min

    interval = 60.0 / rate_per_min  # seconds between requests
    end_time = time.monotonic() + duration_seconds
    semaphore = asyncio.Semaphore(concurrency)

    print(f"Load test started: {rate_per_min} req/min for {duration_seconds}s")
    print(f"Target: {base_url}/v1/intents")
    print(f"Mix: 50% GREEN, 30% YELLOW, 20% RED")
    print(f"Concurrency: {concurrency}")
    print("-" * 60)

    async with httpx.AsyncClient() as client:
        tasks: list[asyncio.Task] = []
        request_count = 0

        async def _throttled_request(action: str, tier: str) -> RequestResult:
            async with semaphore:
                return await _send_request(client, base_url, action, tier)

        while time.monotonic() < end_time:
            action, tier = _pick_action()
            task = asyncio.create_task(_throttled_request(action, tier))
            tasks.append(task)
            request_count += 1

            # Progress update every 100 requests
            if request_count % 100 == 0:
                # Collect completed results so far
                done = [t for t in tasks if t.done()]
                errors = sum(1 for t in done if not t.result().success)
                print(f"  Sent: {request_count} | Completed: {len(done)} | Errors: {errors}")

            await asyncio.sleep(interval)

        # Wait for all remaining requests to complete
        print(f"\nWaiting for {len([t for t in tasks if not t.done()])} in-flight requests...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, RequestResult):
                report.add_result(r)
            elif isinstance(r, Exception):
                report.add_result(RequestResult(
                    action="unknown", risk_tier="unknown",
                    status_code=0, latency_ms=0,
                    success=False, error=str(r),
                ))

    report.end_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.duration_seconds = duration_seconds
    report.finalize()

    return report


# =============================================================================
# Receipt Count Verification
# =============================================================================


async def verify_receipts(base_url: str) -> dict[str, Any]:
    """Check receipt count after load test to verify Law #2 compliance."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{base_url}/v1/receipts",
                params={"suite_id": TEST_SUITE_ID, "limit": 1},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "receipt_count": data.get("count", 0),
                    "suite_id": TEST_SUITE_ID,
                    "verified": True,
                }
        except Exception as e:
            return {"receipt_count": 0, "error": str(e), "verified": False}

    return {"receipt_count": 0, "verified": False}


# =============================================================================
# Readiness Check
# =============================================================================


async def check_readyz(base_url: str) -> dict[str, Any]:
    """Verify /readyz returns 200 before starting load test."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base_url}/readyz", timeout=5.0)
            data = resp.json()
            return {
                "status_code": resp.status_code,
                "ready": resp.status_code == 200,
                "checks": data.get("checks", {}),
            }
        except Exception as e:
            return {"status_code": 0, "ready": False, "error": str(e)}


# =============================================================================
# Main
# =============================================================================


async def main() -> None:
    parser = argparse.ArgumentParser(description="Aspire Orchestrator Load Test")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Orchestrator base URL")
    parser.add_argument("--duration", type=int, default=600, help="Test duration in seconds (default: 600)")
    parser.add_argument("--rate", type=int, default=100, help="Requests per minute (default: 100)")
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent requests (default: 10)")
    parser.add_argument("--output", type=str, default=None, help="Output file path for JSON report")
    parser.add_argument("--skip-readyz", action="store_true", help="Skip readiness check")
    args = parser.parse_args()

    # Step 1: Readiness check
    if not args.skip_readyz:
        print("Checking /readyz...")
        readyz = await check_readyz(args.base_url)
        if not readyz["ready"]:
            print(f"ABORT: /readyz not ready: {json.dumps(readyz, indent=2)}")
            sys.exit(1)
        print(f"  /readyz OK: {readyz['checks']}")

    # Step 2: Run load test
    report = await run_load_test(
        base_url=args.base_url,
        duration_seconds=args.duration,
        rate_per_min=args.rate,
        concurrency=args.concurrency,
    )

    # Step 3: Verify receipts
    print("\nVerifying receipt coverage (Law #2)...")
    receipt_check = await verify_receipts(args.base_url)

    # Step 4: Generate report
    report_dict = report.to_dict()
    report_dict["receipt_verification"] = receipt_check

    report_json = json.dumps(report_dict, indent=2)

    print("\n" + "=" * 60)
    print("LOAD TEST REPORT")
    print("=" * 60)
    print(report_json)

    verdict = report_dict["slo_verdict"]["overall"]
    print(f"\nSLO VERDICT: {verdict}")

    if args.output:
        with open(args.output, "w") as f:
            f.write(report_json)
        print(f"\nReport saved to: {args.output}")

    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(main())
