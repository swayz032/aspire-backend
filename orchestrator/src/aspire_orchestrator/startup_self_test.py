"""Startup self-test — runs at lifespan boot, gates /readyz on failure.

F-ARCH-1: prior production incidents (Ava Offline 2026-03-27, sparse
show_cards 2026-04-15) traced back to silent startup misconfigurations —
missing prompt sections, undersized signing keys, drifted store directory,
unset provider keys. Each was discovered hours later via user complaints
because /readyz only verified that processes were RUNNING, not that they
were CORRECT.

This module runs five categorical checks at process startup:

  1. Bundled Ava prompt is present + contains required sections
     (BROWSE MODE, # Tools — these are the rule anchors the LLM relies on).
  2. HD store directory loaded and has > 1500 stores (current canonical
     count is ~2317; >1500 catches catastrophic data drift, e.g. an empty
     refresh PR landing).
  3. Required env vars are present.
  4. Capability token signing key is at least 32 bytes (HMAC-SHA256 / RFC
     7518 §3.2 minimum).
  5. Bundled prompt SHA-256 matches a recorded canonical hash, IF a
     canonical hash file is present alongside the prompt. We log the
     observed hash even when no canonical exists, so operators can seed
     the file from the deploy log on first boot.

The result is a dict consumed by /readyz; failures cause /readyz to return
503 with the failing check names.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Required env vars. Each appears in at least one production code path
# (validated against grep at the time of writing). Missing any in
# production = fail-closed.
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "ASPIRE_TOKEN_SIGNING_KEY",
    "GOOGLE_MAPS_API_KEY",
    "ANAM_API_KEY",
    "ASPIRE_TOOL_SECRET",
    "OPENAI_API_KEY",
    "SERPAPI_API_KEY",
    "ATTOM_API_KEY",
)

# Where the bundled Ava prompt lives. The file is committed; production
# secrets are not in it, so we can hash it deterministically.
_AVA_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "pack_personas"
    / "ava_anam_video_prompt.md"
)

# Optional sidecar: if present, must match the live file's SHA-256. The
# sidecar is updated by the deployment pipeline whenever the prompt
# changes. Mismatch = drift between source-of-truth (the .md) and a stale
# bundled artifact.
_AVA_PROMPT_HASH_PATH = _AVA_PROMPT_PATH.with_suffix(".md.sha256")

# Required prompt section anchors. The LLM reads these as rule headers.
_REQUIRED_PROMPT_SECTIONS: tuple[str, ...] = (
    "BROWSE MODE",
    "# Tools",
)

# Canonical lower bound for the HD store directory. Set well below the
# actual ~2317 figure so a small monthly drift PR doesn't trip the gate;
# the upper bound (eg. >5000 = bogus growth) is not enforced because the
# refresh PR review covers that case.
_HD_STORE_MIN_COUNT = 1500

# Token signing key minimum length (RFC 7518 §3.2 requires >= 32 bytes
# for HMAC-SHA256).
_TOKEN_SIGNING_KEY_MIN_LENGTH = 32


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    fatal: bool = True


@dataclass
class SelfTestReport:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    prompt_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "prompt_sha256": self.prompt_sha256,
        }


def _check_prompt_file() -> tuple[CheckResult, str]:
    """Verify Ava prompt exists and contains required sections. Returns the SHA-256."""
    try:
        text = _AVA_PROMPT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        return (
            CheckResult(
                name="ava_prompt_present",
                passed=False,
                detail=f"unreadable at {_AVA_PROMPT_PATH}: {exc}",
            ),
            "",
        )

    missing = [section for section in _REQUIRED_PROMPT_SECTIONS if section not in text]
    if missing:
        return (
            CheckResult(
                name="ava_prompt_sections",
                passed=False,
                detail=f"missing sections: {missing}",
            ),
            "",
        )

    prompt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (
        CheckResult(
            name="ava_prompt_sections",
            passed=True,
            detail=f"sha256={prompt_hash[:12]}...",
        ),
        prompt_hash,
    )


def _check_prompt_hash_parity(observed_hash: str) -> CheckResult:
    """If a canonical hash sidecar exists, observed must match. Otherwise pass."""
    if not observed_hash:
        return CheckResult(
            name="ava_prompt_hash_parity",
            passed=False,
            detail="no observed hash (prompt file failed earlier check)",
        )
    if not _AVA_PROMPT_HASH_PATH.exists():
        return CheckResult(
            name="ava_prompt_hash_parity",
            passed=True,
            detail=(
                f"no canonical sidecar at {_AVA_PROMPT_HASH_PATH.name}; "
                f"observed={observed_hash[:12]}... (seed sidecar from this hash)"
            ),
            fatal=False,
        )
    try:
        canonical = _AVA_PROMPT_HASH_PATH.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError) as exc:
        return CheckResult(
            name="ava_prompt_hash_parity",
            passed=False,
            detail=f"sidecar unreadable: {exc}",
        )
    if canonical.lower() != observed_hash.lower():
        return CheckResult(
            name="ava_prompt_hash_parity",
            passed=False,
            detail=(
                f"hash drift — canonical={canonical[:12]}... "
                f"observed={observed_hash[:12]}..."
            ),
        )
    return CheckResult(
        name="ava_prompt_hash_parity",
        passed=True,
        detail=f"matches canonical={canonical[:12]}...",
    )


def _check_hd_store_directory() -> CheckResult:
    """Verify the HD store directory loads with at least the minimum store count."""
    try:
        from aspire_orchestrator.services.adam.hd_store_directory import directory_size

        count = directory_size()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="hd_store_directory",
            passed=False,
            detail=f"load failed: {exc}",
        )
    if count < _HD_STORE_MIN_COUNT:
        return CheckResult(
            name="hd_store_directory",
            passed=False,
            detail=f"count={count} below floor {_HD_STORE_MIN_COUNT}",
        )
    return CheckResult(
        name="hd_store_directory",
        passed=True,
        detail=f"count={count}",
    )


def _check_required_env_vars() -> CheckResult:
    """Verify each required env var is non-empty."""
    missing: list[str] = []
    for name in _REQUIRED_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if not value:
            missing.append(name)
    if missing:
        return CheckResult(
            name="required_env_vars",
            passed=False,
            detail=f"missing: {missing}",
        )
    return CheckResult(
        name="required_env_vars",
        passed=True,
        detail=f"all {len(_REQUIRED_ENV_VARS)} present",
    )


def _check_token_signing_key_length() -> CheckResult:
    """Capability token signing key must be at least 32 bytes for HMAC-SHA256."""
    key = (
        os.environ.get("ASPIRE_TOKEN_SIGNING_KEY")
        or ""
    ).strip()
    if not key:
        return CheckResult(
            name="token_signing_key_length",
            passed=False,
            detail="ASPIRE_TOKEN_SIGNING_KEY is empty",
        )
    if key == "UNCONFIGURED-FAIL-CLOSED":
        return CheckResult(
            name="token_signing_key_length",
            passed=False,
            detail="signing key is the fail-closed sentinel",
        )
    if len(key) < _TOKEN_SIGNING_KEY_MIN_LENGTH:
        return CheckResult(
            name="token_signing_key_length",
            passed=False,
            detail=(
                f"length={len(key)} bytes below RFC 7518 minimum "
                f"({_TOKEN_SIGNING_KEY_MIN_LENGTH})"
            ),
        )
    return CheckResult(
        name="token_signing_key_length",
        passed=True,
        detail=f"length={len(key)} bytes",
    )


def run_self_test() -> SelfTestReport:
    """Run all checks and return a report.

    Logs each check at INFO on success / ERROR on failure. Callers wire the
    report into /readyz so the platform observes failures without needing
    to scrape logs.
    """
    checks: list[CheckResult] = []

    prompt_check, prompt_hash = _check_prompt_file()
    checks.append(prompt_check)
    checks.append(_check_prompt_hash_parity(prompt_hash))
    checks.append(_check_hd_store_directory())
    checks.append(_check_required_env_vars())
    checks.append(_check_token_signing_key_length())

    fatal_failures = [c.name for c in checks if not c.passed and c.fatal]
    soft_failures = [c.name for c in checks if not c.passed and not c.fatal]

    for c in checks:
        if c.passed:
            logger.info("[startup_self_test] PASS %s — %s", c.name, c.detail)
        elif c.fatal:
            logger.error("[startup_self_test] FAIL %s — %s", c.name, c.detail)
        else:
            logger.warning("[startup_self_test] WARN %s — %s", c.name, c.detail)

    report = SelfTestReport(
        passed=not fatal_failures,
        checks=checks,
        failures=fatal_failures + soft_failures,
        prompt_sha256=prompt_hash,
    )
    return report


# Module-level cache so /readyz can read the last result without re-running.
_LAST_REPORT: SelfTestReport | None = None


def get_last_report() -> SelfTestReport | None:
    return _LAST_REPORT


def set_last_report(report: SelfTestReport) -> None:
    global _LAST_REPORT
    _LAST_REPORT = report
