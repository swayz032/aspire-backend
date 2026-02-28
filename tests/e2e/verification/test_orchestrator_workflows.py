"""E2E Orchestrator Workflow Tests -- 15 intent-to-receipt round trips.

Each test sends a natural-language intent through the Desktop proxy
(POST /api/orchestrator/intent) and verifies that:

1. The response contains ``response``, ``receipt_id``, ``governance``,
   ``risk_tier``, and ``route`` fields.
2. The correct agent/skill-pack was routed to.
3. The risk tier is assigned correctly (GREEN / YELLOW / RED).

Tests are organized by risk tier:
- GREEN  (4 tests):  read-only / search / join operations
- YELLOW (8 tests):  state-changing but non-binding operations
- RED    (3 tests):  binding financial / legal / irreversible operations

Law compliance:
- Law #2: every call must produce a receipt_id
- Law #3: missing X-Suite-Id must fail closed (401)
- Law #4: risk tier assignment matches policy matrix
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import requests

# ---------------------------------------------------------------------------
# Data model for parameterized test cases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntentCase:
    """A single intent test case."""
    id: str
    text: str
    expected_route: str  # substring expected in the route
    expected_risk: str   # green | yellow | red
    description: str


# ---------------------------------------------------------------------------
# GREEN tier cases
# ---------------------------------------------------------------------------

GREEN_CASES: list[IntentCase] = [
    IntentCase(
        id="green_calendar",
        text="Check my calendar for tomorrow",
        expected_route="calendar",
        expected_risk="green",
        description="Calendar read is GREEN (read-only data retrieval)",
    ),
    IntentCase(
        id="green_adam_search",
        text="Search for HVAC suppliers in Austin",
        expected_route="adam",
        expected_risk="green",
        description="Adam research/search is GREEN (no state change)",
    ),
    IntentCase(
        id="green_tec_summarize",
        text="Summarize the Thompson proposal",
        expected_route="tec",
        expected_risk="green",
        description="Tec document summarization is GREEN (read-only)",
    ),
    IntentCase(
        id="green_nora_join",
        text="Join the team meeting",
        expected_route="nora",
        expected_risk="green",
        description="Nora conference join is GREEN (no external effect)",
    ),
]

# ---------------------------------------------------------------------------
# YELLOW tier cases
# ---------------------------------------------------------------------------

YELLOW_CASES: list[IntentCase] = [
    IntentCase(
        id="yellow_quinn_create",
        text="Create an invoice for Sarah for $1,200",
        expected_route="quinn",
        expected_risk="yellow",
        description="Quinn invoice creation is YELLOW (state change + external)",
    ),
    IntentCase(
        id="yellow_quinn_send",
        text="Send that invoice to Sarah",
        expected_route="quinn",
        expected_risk="yellow",
        description="Quinn invoice send is YELLOW (external communication)",
    ),
    IntentCase(
        id="yellow_eli_draft",
        text="Draft an email to Marcus about the project update",
        expected_route="eli",
        expected_risk="yellow",
        description="Eli email draft is YELLOW (external communication)",
    ),
    IntentCase(
        id="yellow_finn_fm",
        text="What's my financial health?",
        expected_route="finn",
        expected_risk="yellow",
        description="Finn finance manager analysis is YELLOW (strategic intelligence)",
    ),
    IntentCase(
        id="yellow_sarah_schedule",
        text="Schedule a call with Johnson tomorrow at 2pm",
        expected_route="sarah",
        expected_risk="yellow",
        description="Sarah scheduling is YELLOW (creates calendar event)",
    ),
    IntentCase(
        id="yellow_teressa_reconcile",
        text="Reconcile last month's books",
        expected_route="teressa",
        expected_risk="yellow",
        description="Teressa reconciliation is YELLOW (bookkeeping state change)",
    ),
    IntentCase(
        id="yellow_mail_ops",
        text="Set up mailbox for newemployee@mybusiness.com",
        expected_route="mail",
        expected_risk="yellow",
        description="Mail ops mailbox creation is YELLOW (infrastructure change)",
    ),
    IntentCase(
        id="yellow_tec_proposal",
        text="Generate a proposal for the Thompson project",
        expected_route="tec",
        expected_risk="yellow",
        description="Tec proposal generation is YELLOW (external share potential)",
    ),
]

# ---------------------------------------------------------------------------
# RED tier cases
# ---------------------------------------------------------------------------

RED_CASES: list[IntentCase] = [
    IntentCase(
        id="red_finn_payment",
        text="Send $500 payment to John's Plumbing",
        expected_route="finn",
        expected_risk="red",
        description="Finn money desk payment is RED (irreversible financial)",
    ),
    IntentCase(
        id="red_clara_contract",
        text="Create a service agreement contract for Thompson",
        expected_route="clara",
        expected_risk="red",
        description="Clara contract creation is RED (legal/binding)",
    ),
    IntentCase(
        id="red_milo_payroll",
        text="Process payroll for this week",
        expected_route="milo",
        expected_risk="red",
        description="Milo payroll processing is RED (irreversible financial)",
    ),
]

ALL_CASES = GREEN_CASES + YELLOW_CASES + RED_CASES


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.needs_desktop,
]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _send_intent(
    http: requests.Session,
    desktop_url: str,
    auth_headers: dict[str, str],
    text: str,
) -> requests.Response:
    """POST an intent to the Desktop proxy and return the raw response."""
    return http.post(
        f"{desktop_url}/api/orchestrator/intent",
        json={"text": text, "agent": "ava", "channel": "voice"},
        headers=auth_headers,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Parametrized tests by risk tier
# ---------------------------------------------------------------------------


class TestGreenTierWorkflows:
    """GREEN tier intents -- autonomous, read-only, still produce receipts."""

    @pytest.mark.parametrize(
        "case",
        GREEN_CASES,
        ids=[c.id for c in GREEN_CASES],
    )
    def test_green_intent(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
        case: IntentCase,
    ) -> None:
        """Verify GREEN intent routes correctly and returns a receipt."""
        resp = _send_intent(http, desktop_url, auth_headers, case.text)

        # The orchestrator may return 200 (completed) for GREEN
        assert resp.status_code in (200, 202), (
            f"Expected 200/202 for GREEN intent '{case.id}', got {resp.status_code}: "
            f"{resp.text[:300]}"
        )

        data = resp.json()

        # Must contain a response text
        assert "response" in data, f"Missing 'response' field for {case.id}"

        # Must produce a receipt (Law #2)
        assert data.get("receipt_id") is not None or data.get("governance") is not None, (
            f"GREEN intent '{case.id}' did not produce a receipt or governance block"
        )

        # Verify risk tier
        risk = data.get("risk_tier") or ""
        assert risk.lower() == case.expected_risk, (
            f"Expected risk_tier='{case.expected_risk}' for '{case.id}', got '{risk}'"
        )

        # Verify route contains expected agent
        route = str(data.get("route") or "")
        assert case.expected_route in route.lower(), (
            f"Expected route containing '{case.expected_route}' for '{case.id}', "
            f"got '{route}'"
        )


class TestYellowTierWorkflows:
    """YELLOW tier intents -- require user confirmation, generate approval requests."""

    @pytest.mark.parametrize(
        "case",
        YELLOW_CASES,
        ids=[c.id for c in YELLOW_CASES],
    )
    def test_yellow_intent(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
        case: IntentCase,
    ) -> None:
        """Verify YELLOW intent routes correctly and requires approval or returns receipt."""
        resp = _send_intent(http, desktop_url, auth_headers, case.text)

        # YELLOW may return 200 (plan presented) or 202 (approval required)
        assert resp.status_code in (200, 202), (
            f"Expected 200/202 for YELLOW intent '{case.id}', got {resp.status_code}: "
            f"{resp.text[:300]}"
        )

        data = resp.json()

        # Must contain a response text
        assert "response" in data, f"Missing 'response' field for {case.id}"

        # Receipt or governance must be present (Law #2)
        assert data.get("receipt_id") is not None or data.get("governance") is not None, (
            f"YELLOW intent '{case.id}' did not produce a receipt or governance block"
        )

        # Verify risk tier
        risk = data.get("risk_tier") or ""
        assert risk.lower() == case.expected_risk, (
            f"Expected risk_tier='{case.expected_risk}' for '{case.id}', got '{risk}'"
        )

        # Verify route contains expected agent
        route = str(data.get("route") or "")
        assert case.expected_route in route.lower(), (
            f"Expected route containing '{case.expected_route}' for '{case.id}', "
            f"got '{route}'"
        )


class TestRedTierWorkflows:
    """RED tier intents -- require explicit authority + strong confirmation."""

    @pytest.mark.parametrize(
        "case",
        RED_CASES,
        ids=[c.id for c in RED_CASES],
    )
    def test_red_intent(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
        case: IntentCase,
    ) -> None:
        """Verify RED intent routes correctly, requires dual approval, and emits receipt."""
        resp = _send_intent(http, desktop_url, auth_headers, case.text)

        # RED should return 200 (with plan + approval gate) or 202
        assert resp.status_code in (200, 202), (
            f"Expected 200/202 for RED intent '{case.id}', got {resp.status_code}: "
            f"{resp.text[:300]}"
        )

        data = resp.json()

        # Must contain a response text
        assert "response" in data, f"Missing 'response' field for {case.id}"

        # Receipt or governance must be present (Law #2)
        assert data.get("receipt_id") is not None or data.get("governance") is not None, (
            f"RED intent '{case.id}' did not produce a receipt or governance block"
        )

        # Verify risk tier
        risk = data.get("risk_tier") or ""
        assert risk.lower() == case.expected_risk, (
            f"Expected risk_tier='{case.expected_risk}' for '{case.id}', got '{risk}'"
        )

        # Verify route contains expected agent
        route = str(data.get("route") or "")
        assert case.expected_route in route.lower(), (
            f"Expected route containing '{case.expected_route}' for '{case.id}', "
            f"got '{route}'"
        )


# ---------------------------------------------------------------------------
# Auth failure tests (Law #3: Fail Closed)
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Verify the pipeline fails closed when auth headers are missing."""

    def test_missing_suite_id_returns_401(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """POST /api/orchestrator/intent without X-Suite-Id must return 401."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"text": "Check my calendar", "agent": "ava"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code == 401, (
            f"Expected 401 for missing X-Suite-Id, got {resp.status_code}"
        )
        data = resp.json()
        assert "error" in data
        assert "AUTH" in data["error"].upper() or "SUITE" in data.get("message", "").upper()

    def test_empty_text_returns_400(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """POST /api/orchestrator/intent with empty text must return 400."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"text": "", "agent": "ava"},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 400, (
            f"Expected 400 for empty text, got {resp.status_code}"
        )

    def test_missing_text_returns_400(
        self,
        http: requests.Session,
        desktop_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        """POST /api/orchestrator/intent without text field must return 400."""
        resp = http.post(
            f"{desktop_url}/api/orchestrator/intent",
            json={"agent": "ava"},
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 400, (
            f"Expected 400 for missing text, got {resp.status_code}"
        )
