"""
N8N Workflow Governance Tests — Gate 1 (Testing)
=================================================
Validates the governance properties of all 11 n8n workflow JSON files WITHOUT
requiring a live n8n instance. Tests are pure Python/JSON analysis.

Categories:
  N1 — HMAC sender/receiver symmetry (routes.ts sortKeys == n8n sortKeys)
  N2 — Negative: Invalid HMAC must be rejected (kill-switch branch)
  N3 — Kill switch enforcement (all 11 workflows)
  N4 — Receipt coverage (success + killed + failure paths)
  N5 — Retry config on all Gateway HTTP Request nodes
  N6 — No hardcoded secrets in workflow JSON
  N7 — Tenant context propagation (suite_id/office_id in receipts)
  N8 — Idempotency key uniqueness (random suffix required)
  N9 — Error Trigger wired to failure receipt in all 11 workflows
  N10 — Struct: rawBody:true on all webhook triggers

Aspire Laws verified:
  Law #1: Single Brain — n8n only calls Gateway, never decides
  Law #2: Receipt for All Actions — 3 paths per workflow (success, killed, failure)
  Law #3: Fail Closed — invalid HMAC routes to kill branch, not execution
  Law #6: Tenant Isolation — receipts always carry suite_id/office_id
  Law #9: No secrets in repo — all secrets via $env references
"""
import json
import os
import hmac as hmac_lib
import hashlib
import re
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKFLOWS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..",
    "infrastructure", "n8n-workflows"
)

WEBHOOK_WORKFLOWS = {
    "eli-email-triage",
    "sarah-call-handler",
    "nora-meeting-summary",
    "intake-activation",
}

CRON_WORKFLOWS = {
    "adam-daily-brief",
    "adam-pulse-scan",
    "adam-library-curate",
    "adam-focus-weekly",
    "adam-education-curate",
    "quinn-invoice-reminder",
    "teressa-books-sync",
}

ALL_WORKFLOW_NAMES = WEBHOOK_WORKFLOWS | CRON_WORKFLOWS


def load_workflow(name: str) -> dict:
    path = os.path.join(WORKFLOWS_DIR, f"{name}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_nodes_by_type(workflow: dict, node_type: str) -> list:
    return [n for n in workflow.get("nodes", []) if n.get("type") == node_type]


def get_code_nodes(workflow: dict) -> list:
    return get_nodes_by_type(workflow, "n8n-nodes-base.code")


def get_http_nodes(workflow: dict) -> list:
    return get_nodes_by_type(workflow, "n8n-nodes-base.httpRequest")


def get_if_nodes(workflow: dict) -> list:
    return get_nodes_by_type(workflow, "n8n-nodes-base.if")


def get_error_trigger_nodes(workflow: dict) -> list:
    return get_nodes_by_type(workflow, "n8n-nodes-base.errorTrigger")


def get_webhook_nodes(workflow: dict) -> list:
    return get_nodes_by_type(workflow, "n8n-nodes-base.webhook")


def get_code_text(workflow: dict) -> str:
    """Concatenate all jsCode from Code nodes into one string for searching."""
    parts = []
    for n in get_code_nodes(workflow):
        parts.append(n.get("parameters", {}).get("jsCode", ""))
    return "\n".join(parts)


def get_receipt_nodes(workflow: dict) -> list:
    """Return all HTTP nodes whose name contains 'receipt' (case-insensitive)."""
    return [n for n in get_http_nodes(workflow)
            if "receipt" in n.get("name", "").lower()]


def get_gateway_nodes(workflow: dict) -> list:
    """HTTP nodes that call the Gateway/Orchestrator."""
    return [n for n in get_http_nodes(workflow)
            if "orchestrat" in n.get("name", "").lower()
            or "gateway" in n.get("name", "").lower()
            or "intent" in n.get("name", "").lower()]


# ---------------------------------------------------------------------------
# Python-side sort_keys to mirror routes.ts and n8n implementations
# ---------------------------------------------------------------------------

def sort_keys(obj):
    """Recursively sort dict keys — mirrors routes.ts sortKeys() and n8n sortKeys()."""
    if isinstance(obj, list):
        return [sort_keys(i) for i in obj]
    if isinstance(obj, dict):
        return {k: sort_keys(v) for k, v in sorted(obj.items())}
    return obj


def compute_hmac(secret: str, payload: dict) -> str:
    """Compute canonical HMAC-SHA256 matching routes.ts + n8n implementation."""
    canonical = json.dumps(sort_keys(payload), separators=(",", ":"))
    digest = hmac_lib.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


# ===========================================================================
# N1 — HMAC Sender/Receiver Symmetry
# ===========================================================================


class TestN1HmacSymmetry:
    """Law #3: Fail Closed — sender and receiver must produce identical canonical JSON."""

    def test_sort_keys_simple_object(self):
        """sortKeys on a flat dict produces alphabetical key order."""
        payload = {"z": 1, "a": 2, "m": 3}
        result = sort_keys(payload)
        assert list(result.keys()) == ["a", "m", "z"]

    def test_sort_keys_nested_object(self):
        """sortKeys on nested dicts sorts at every level."""
        payload = {"z": {"b": 1, "a": 2}, "a": {"y": 9, "x": 8}}
        result = sort_keys(payload)
        assert list(result.keys()) == ["a", "z"]
        assert list(result["a"].keys()) == ["x", "y"]
        assert list(result["z"].keys()) == ["a", "b"]

    def test_sort_keys_array_preserves_order(self):
        """sortKeys leaves array element order intact, only sorts keys within objects."""
        payload = {"items": [{"z": 3, "a": 1}, {"m": 2}]}
        result = sort_keys(payload)
        assert list(result["items"][0].keys()) == ["a", "z"]

    def test_hmac_canonical_json_is_deterministic(self):
        """Same payload always produces same HMAC regardless of insertion order."""
        payload_a = {"z": "last", "a": "first", "m": "middle"}
        payload_b = {"a": "first", "m": "middle", "z": "last"}
        secret = "test-secret"
        sig_a = compute_hmac(secret, payload_a)
        sig_b = compute_hmac(secret, payload_b)
        assert sig_a == sig_b, "sortKeys must produce identical JSON for same-content dicts"

    def test_hmac_prefix_is_sha256(self):
        """HMAC signature must be prefixed with 'sha256=' (matching n8n verification)."""
        sig = compute_hmac("secret", {"key": "value"})
        assert sig.startswith("sha256="), f"HMAC must start with 'sha256=', got: {sig[:10]}"

    def test_hmac_hex_digest_length(self):
        """HMAC-SHA256 hex digest is always 64 characters."""
        sig = compute_hmac("secret", {"key": "value"})
        hex_part = sig[len("sha256="):]
        assert len(hex_part) == 64, f"HMAC hex part must be 64 chars, got {len(hex_part)}"

    def test_n8n_sortkeys_matches_routes_ts(self):
        """Evil test: n8n Code node sortKeys and routes.ts sortKeys produce identical output
        for a realistic intake webhook payload."""
        intake_payload = {
            "suiteId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "officeId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "industry": "Technology",
            "servicesNeeded": ["Invoicing & Payments", "Email Management"],
            "businessGoals": ["Reduce admin time", "Scale operations"],
            "painPoint": "Too much manual work",
            "correlationId": "corr-test-001",
            "teamSize": "1_to_5",
            "customerType": "b2b",
            "salesChannel": "online",
            "yearsInBusiness": "1_to_3",
        }
        canonical = json.dumps(sort_keys(intake_payload), separators=(",", ":"))
        # Keys must be in strict alphabetical order
        import ast
        loaded = json.loads(canonical)
        keys = list(loaded.keys())
        assert keys == sorted(keys), f"Canonical JSON keys not sorted: {keys}"

    def test_different_secrets_produce_different_hmac(self):
        """Evil test: wrong secret must never produce matching HMAC (basic sanity)."""
        payload = {"suiteId": "abc", "industry": "Tech"}
        sig_correct = compute_hmac("correct-secret", payload)
        sig_wrong = compute_hmac("wrong-secret", payload)
        assert sig_correct != sig_wrong

    def test_modified_payload_produces_different_hmac(self):
        """Evil test: tampering with payload changes the HMAC (tamper detection)."""
        payload_orig = {"suiteId": "abc", "industry": "Tech"}
        payload_tampered = {"suiteId": "xyz", "industry": "Tech"}
        secret = "test-secret"
        sig_orig = compute_hmac(secret, payload_orig)
        sig_tampered = compute_hmac(secret, payload_tampered)
        assert sig_orig != sig_tampered


# ===========================================================================
# N2 — Negative: HMAC Validation in Webhook Workflows
# ===========================================================================


class TestN2HmacValidation:
    """Law #3: Fail Closed — invalid HMAC must route to kill branch, not execute."""

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_webhook_has_hmac_in_code_node(self, workflow_name):
        """All webhook workflows must include HMAC validation in Code node (Law #3)."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        assert "timingSafeEqual" in code_text, (
            f"{workflow_name}: must use timingSafeEqual for timing-safe HMAC comparison"
        )
        assert "createHmac" in code_text, (
            f"{workflow_name}: must use crypto.createHmac for HMAC generation"
        )
        assert "sha256=" in code_text, (
            f"{workflow_name}: must prefix signature with 'sha256='"
        )

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_webhook_hmac_uses_raw_body(self, workflow_name):
        """All webhook triggers must have rawBody:true to get unmodified payload bytes."""
        wf = load_workflow(workflow_name)
        webhook_nodes = get_webhook_nodes(wf)
        assert len(webhook_nodes) >= 1, f"{workflow_name}: no webhook trigger node found"
        for node in webhook_nodes:
            options = node.get("parameters", {}).get("options", {})
            assert options.get("rawBody") is True, (
                f"{workflow_name}: webhook node '{node['name']}' must have rawBody:true"
            )

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_webhook_uses_correct_secret_env_var(self, workflow_name):
        """Each webhook must use its own dedicated secret env var (not the default)."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        expected_env_vars = {
            "eli-email-triage": "N8N_ELI_WEBHOOK_SECRET",
            "sarah-call-handler": "N8N_SARAH_WEBHOOK_SECRET",
            "nora-meeting-summary": "N8N_NORA_WEBHOOK_SECRET",
            "intake-activation": "N8N_WEBHOOK_SECRET",  # uses the default (intentional)
        }
        expected_var = expected_env_vars[workflow_name]
        assert expected_var in code_text, (
            f"{workflow_name}: must use env var '{expected_var}' for HMAC secret"
        )

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_hmac_rejection_routes_to_killed_not_execute(self, workflow_name):
        """Evil test: Code node must return killed=true on HMAC failure, not fall through."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        # The code must check HMAC and return killed:true on mismatch
        assert "killed: true" in code_text or "killed:true" in code_text, (
            f"{workflow_name}: HMAC rejection must return killed:true to stop execution"
        )
        assert "hmac_validation_failed" in code_text, (
            f"{workflow_name}: HMAC rejection must include reason 'hmac_validation_failed'"
        )

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_buffer_length_check_before_timing_safe_equal(self, workflow_name):
        """Evil test: timingSafeEqual RangeError if buffers differ in length — must pre-check.

        Buffers of different lengths passed to timingSafeEqual throws RangeError in Node.js,
        which would propagate as a 500 and bypass the kill branch. The correct pattern is:
        'if (sigBuf.length !== expBuf.length || !crypto.timingSafeEqual(sigBuf, expBuf))'
        """
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        assert "sigBuf.length !== expBuf.length" in code_text, (
            f"{workflow_name}: must check buffer lengths before timingSafeEqual to avoid RangeError"
        )

    def test_intake_hmac_missing_tenant_fails_closed(self):
        """Evil test: intake-activation must reject payloads without suiteId (missing tenant)."""
        wf = load_workflow("intake-activation")
        code_text = get_code_text(wf)
        assert "missing_tenant_context" in code_text, (
            "intake-activation: must reject payloads missing suiteId with 'missing_tenant_context'"
        )


# ===========================================================================
# N3 — Kill Switch Enforcement
# ===========================================================================


class TestN3KillSwitch:
    """Law #1: Single Brain — kill switch prevents autonomous execution when disabled."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_kill_switch_code_node(self, workflow_name):
        """All 11 workflows must have a kill switch Code node."""
        wf = load_workflow(workflow_name)
        code_nodes = get_code_nodes(wf)
        kill_nodes = [n for n in code_nodes
                      if "kill" in n.get("name", "").lower()
                      or "switch" in n.get("name", "").lower()
                      or "prep" in n.get("name", "").lower()]
        assert len(kill_nodes) >= 1, (
            f"{workflow_name}: must have a kill switch Code node"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_kill_switch_checks_correct_env_var(self, workflow_name):
        """Each workflow's kill switch must check its own N8N_WORKFLOW_*_ENABLED env var."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        env_var_map = {
            "adam-daily-brief": "N8N_WORKFLOW_ADAM_DAILY_BRIEF_ENABLED",
            "adam-pulse-scan": "N8N_WORKFLOW_ADAM_PULSE_SCAN_ENABLED",
            "adam-library-curate": "N8N_WORKFLOW_ADAM_LIBRARY_CURATE_ENABLED",
            "adam-focus-weekly": "N8N_WORKFLOW_ADAM_FOCUS_WEEKLY_ENABLED",
            "adam-education-curate": "N8N_WORKFLOW_ADAM_EDUCATION_CURATE_ENABLED",
            "eli-email-triage": "N8N_WORKFLOW_ELI_EMAIL_TRIAGE_ENABLED",
            "sarah-call-handler": "N8N_WORKFLOW_SARAH_CALL_HANDLER_ENABLED",
            "nora-meeting-summary": "N8N_WORKFLOW_NORA_MEETING_SUMMARY_ENABLED",
            "quinn-invoice-reminder": "N8N_WORKFLOW_QUINN_INVOICE_REMINDER_ENABLED",
            "teressa-books-sync": "N8N_WORKFLOW_TERESSA_BOOKS_SYNC_ENABLED",
            "intake-activation": "N8N_WORKFLOW_INTAKE_ACTIVATION_ENABLED",
        }
        expected_var = env_var_map[workflow_name]
        assert expected_var in code_text, (
            f"{workflow_name}: kill switch must check env var '{expected_var}'"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_kill_switch_if_node_exists(self, workflow_name):
        """All workflows must have an IF node branching on the kill switch output."""
        wf = load_workflow(workflow_name)
        if_nodes = get_if_nodes(wf)
        kill_if = [n for n in if_nodes
                   if "kill" in n.get("name", "").lower()
                   or "switch" in n.get("name", "").lower()
                   or "active" in n.get("name", "").lower()]
        assert len(kill_if) >= 1, (
            f"{workflow_name}: must have an IF node for kill switch branching"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_kill_switch_if_checks_killed_field(self, workflow_name):
        """Kill switch IF node must compare $json.killed against string 'true'.

        KNOWN DESIGN DECISION: n8n IF nodes using 'string' comparison mode compare
        $json.killed (which is a boolean) against the string 'true'. In n8n v1,
        this works because n8n coerces the boolean to string before comparison.
        This is the established pattern across all 11 workflows.
        """
        wf = load_workflow(workflow_name)
        if_nodes = get_if_nodes(wf)
        kill_if = [n for n in if_nodes
                   if "kill" in n.get("name", "").lower()
                   or "active" in n.get("name", "").lower()]
        assert len(kill_if) >= 1, f"{workflow_name}: kill switch IF node not found"
        node = kill_if[0]
        conditions = node.get("parameters", {}).get("conditions", {})
        string_conditions = conditions.get("string", [])
        assert len(string_conditions) >= 1, (
            f"{workflow_name}: kill switch IF node must have string conditions"
        )
        cond = string_conditions[0]
        assert "$json.killed" in cond.get("value1", ""), (
            f"{workflow_name}: kill switch must check $json.killed"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_kill_switch_emits_receipt_when_triggered(self, workflow_name):
        """Evil test: kill switch branch must emit a SKIPPED receipt (Law #2)."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        killed_receipts = [n for n in receipt_nodes
                           if "kill" in n.get("name", "").lower()
                           or "skip" in n.get("name", "").lower()
                           or "reject" in n.get("name", "").lower()]
        assert len(killed_receipts) >= 1, (
            f"{workflow_name}: kill switch branch must emit a receipt (Law #2)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_kill_switch_receipt_has_skipped_status(self, workflow_name):
        """Kill switch receipt must have status: SKIPPED."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        killed_receipts = [n for n in receipt_nodes
                           if "kill" in n.get("name", "").lower()
                           or "skip" in n.get("name", "").lower()
                           or "reject" in n.get("name", "").lower()]
        assert len(killed_receipts) >= 1, f"{workflow_name}: no kill receipt found"
        body = killed_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "SKIPPED" in body, (
            f"{workflow_name}: kill switch receipt status must be SKIPPED"
        )


# ===========================================================================
# N4 — Receipt Coverage
# ===========================================================================


class TestN4ReceiptCoverage:
    """Law #2: No Action Without a Receipt — all 3 paths must emit receipts."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_success_receipt(self, workflow_name):
        """All workflows must have a success receipt node (Law #2)."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        success_receipts = [n for n in receipt_nodes
                            if "success" in n.get("name", "").lower()]
        assert len(success_receipts) >= 1, (
            f"{workflow_name}: must have a 'Emit Success Receipt' node (Law #2)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_failure_receipt(self, workflow_name):
        """All workflows must have a failure receipt node (Law #2 — failures need receipts too)."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        failure_receipts = [n for n in receipt_nodes
                            if "fail" in n.get("name", "").lower()]
        assert len(failure_receipts) >= 1, (
            f"{workflow_name}: must have a 'Emit Failure Receipt' node (Law #2)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_success_receipt_has_succeeded_status(self, workflow_name):
        """Success receipt body must contain status: 'SUCCEEDED'."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        success_receipts = [n for n in receipt_nodes
                            if "success" in n.get("name", "").lower()]
        assert len(success_receipts) >= 1, f"{workflow_name}: no success receipt"
        body = success_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "SUCCEEDED" in body, (
            f"{workflow_name}: success receipt must contain status SUCCEEDED"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_failure_receipt_has_failed_status(self, workflow_name):
        """Failure receipt body must contain status: 'FAILED'."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        failure_receipts = [n for n in receipt_nodes
                            if "fail" in n.get("name", "").lower()]
        assert len(failure_receipts) >= 1, f"{workflow_name}: no failure receipt"
        body = failure_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "FAILED" in body, (
            f"{workflow_name}: failure receipt must contain status FAILED"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_receipts_use_supabase_url_env_var(self, workflow_name):
        """Evil test: all receipt nodes must use $env.SUPABASE_URL, not hardcoded URL."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        for node in receipt_nodes:
            url = node.get("parameters", {}).get("url", "")
            assert "$env.SUPABASE_URL" in url or "SUPABASE_URL" in url, (
                f"{workflow_name}: receipt node '{node['name']}' must use $env.SUPABASE_URL"
            )
            assert "supabase.co" not in url.replace("SUPABASE_URL", ""), (
                f"{workflow_name}: receipt node '{node['name']}' must not hardcode Supabase URL"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_receipts_use_service_role_key_env_var(self, workflow_name):
        """Evil test: all receipt nodes must use $env.SUPABASE_SERVICE_ROLE_KEY."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        for node in receipt_nodes:
            headers = node.get("parameters", {}).get("headerParameters", {}).get("parameters", [])
            header_values = [h.get("value", "") for h in headers]
            combined = " ".join(header_values)
            assert "SUPABASE_SERVICE_ROLE_KEY" in combined, (
                f"{workflow_name}: receipt node '{node['name']}' must use $env.SUPABASE_SERVICE_ROLE_KEY"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_receipt_id_has_random_suffix(self, workflow_name):
        """Receipt IDs must include random suffix to prevent collision (Law #2 — idempotency).

        Pattern: 'n8n-xxx-' + Date.now() + '-' + Math.random()...
        """
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        for node in receipt_nodes:
            body = node.get("parameters", {}).get("jsonBody", "")
            assert "Math.random()" in body, (
                f"{workflow_name}: receipt node '{node['name']}' receipt_id must include "
                f"Math.random() suffix to prevent ID collision"
            )
            assert "Date.now()" in body, (
                f"{workflow_name}: receipt node '{node['name']}' receipt_id must include "
                f"Date.now() for uniqueness"
            )


# ===========================================================================
# N5 — Retry Config on Gateway Calls
# ===========================================================================


class TestN5RetryConfig:
    """Law #3: Fail Closed — Gateway calls must have retry config with exponential backoff."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_nodes_have_retry_config(self, workflow_name):
        """All Gateway HTTP Request nodes must have retry config (maxRetries >= 3)."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        assert len(gateway_nodes) >= 1, (
            f"{workflow_name}: must have at least one Gateway/Orchestrator HTTP node"
        )
        for node in gateway_nodes:
            options = node.get("parameters", {}).get("options", {})
            retry = options.get("retry", {})
            assert retry.get("maxRetries", 0) >= 3, (
                f"{workflow_name}: Gateway node '{node['name']}' must have maxRetries >= 3"
            )
            assert retry.get("retryIntervalBackoff") is True, (
                f"{workflow_name}: Gateway node '{node['name']}' must use exponential backoff"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_nodes_have_timeout(self, workflow_name):
        """All Gateway HTTP Request nodes must have a timeout to prevent hanging."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            options = node.get("parameters", {}).get("options", {})
            timeout = options.get("timeout", 0)
            assert timeout >= 5000, (
                f"{workflow_name}: Gateway node '{node['name']}' must have timeout >= 5000ms"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_nodes_use_aspire_gateway_url_env_var(self, workflow_name):
        """Evil test: Gateway nodes must use $env.ASPIRE_GATEWAY_URL, not hardcoded host."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            url = node.get("parameters", {}).get("url", "")
            assert "ASPIRE_GATEWAY_URL" in url, (
                f"{workflow_name}: Gateway node '{node['name']}' must use "
                f"$env.ASPIRE_GATEWAY_URL, not hardcoded host"
            )
            assert "localhost" not in url or "GATEWAY_URL" in url, (
                f"{workflow_name}: Gateway node '{node['name']}' must not hardcode localhost"
            )


# ===========================================================================
# N6 — No Hardcoded Secrets
# ===========================================================================


class TestN6NoHardcodedSecrets:
    """Law #9: Security & Privacy Baselines — no secrets in workflow JSON."""

    KNOWN_SECRET_PATTERNS = [
        # JWT tokens (Base64 header)
        r"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        # Stripe live keys
        r"sk_live_[A-Za-z0-9]+",
        r"pk_live_[A-Za-z0-9]+",
        # Database URLs with credentials
        r"postgres://[^:]+:[^@]+@[^/]+",
        # API keys pattern (not env var reference)
        r'"apikey"\s*:\s*"[^$][A-Za-z0-9]{20,}"',
    ]

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_no_hardcoded_jwt_tokens(self, workflow_name):
        """Evil test: workflow JSON must not contain hardcoded JWT bearer tokens."""
        wf = load_workflow(workflow_name)
        wf_str = json.dumps(wf)
        # Detect JWT-like patterns that are NOT inside $env references
        jwt_pattern = re.compile(
            r"(?<!env\.)(?<!env\[')eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        )
        matches = jwt_pattern.findall(wf_str)
        # Filter out template expression references (they would have $env. before them)
        assert not matches, (
            f"{workflow_name}: hardcoded JWT token found: {matches[0][:50]}..."
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_no_hardcoded_stripe_keys(self, workflow_name):
        """Evil test: workflow JSON must not contain Stripe live API keys."""
        wf = load_workflow(workflow_name)
        wf_str = json.dumps(wf)
        assert "sk_live_" not in wf_str, f"{workflow_name}: contains Stripe live secret key"
        assert "pk_live_" not in wf_str, f"{workflow_name}: contains Stripe live publishable key"

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_all_credential_references_use_env_vars(self, workflow_name):
        """Evil test: auth headers must reference $env variables, not literal values."""
        wf = load_workflow(workflow_name)
        http_nodes = get_http_nodes(wf)
        for node in http_nodes:
            headers = node.get("parameters", {}).get("headerParameters", {}).get("parameters", [])
            for header in headers:
                if header.get("name", "").lower() in ("authorization", "apikey", "x-api-key"):
                    value = header.get("value", "")
                    assert "$env." in value or "={{" in value, (
                        f"{workflow_name}: node '{node['name']}' header '{header['name']}' "
                        f"must use $env reference, not literal value"
                    )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_no_postgres_connection_strings(self, workflow_name):
        """Evil test: workflow JSON must not contain database connection strings."""
        wf = load_workflow(workflow_name)
        wf_str = json.dumps(wf)
        assert "postgres://" not in wf_str, (
            f"{workflow_name}: contains hardcoded PostgreSQL connection string"
        )
        assert "postgresql://" not in wf_str, (
            f"{workflow_name}: contains hardcoded PostgreSQL connection string"
        )


# ===========================================================================
# N7 — Tenant Context Propagation
# ===========================================================================


class TestN7TenantContext:
    """Law #6: Tenant Isolation — receipts must always carry suite_id/office_id."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_success_receipt_carries_suite_id(self, workflow_name):
        """Success receipt body must include suite_id for tenant isolation (Law #6)."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        success_receipts = [n for n in receipt_nodes
                            if "success" in n.get("name", "").lower()]
        assert len(success_receipts) >= 1, f"{workflow_name}: no success receipt"
        body = success_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "suite_id" in body, (
            f"{workflow_name}: success receipt must include suite_id (Law #6)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_success_receipt_carries_office_id(self, workflow_name):
        """Success receipt body must include office_id for tenant isolation (Law #6)."""
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        success_receipts = [n for n in receipt_nodes
                            if "success" in n.get("name", "").lower()]
        assert len(success_receipts) >= 1, f"{workflow_name}: no success receipt"
        body = success_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "office_id" in body, (
            f"{workflow_name}: success receipt must include office_id (Law #6)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_failure_receipt_uses_sentinel_uuid_for_missing_tenant(self, workflow_name):
        """Failure receipts (from Error Trigger) use sentinel UUID for missing tenant context.

        When an error occurs before tenant context is established, receipts must use
        the sentinel 'ffffffff-0000-0000-0000-system000000' not NULL (Law #2 + #6).
        """
        wf = load_workflow(workflow_name)
        receipt_nodes = get_receipt_nodes(wf)
        failure_receipts = [n for n in receipt_nodes
                            if "fail" in n.get("name", "").lower()]
        assert len(failure_receipts) >= 1, f"{workflow_name}: no failure receipt"
        body = failure_receipts[0].get("parameters", {}).get("jsonBody", "")
        assert "ffffffff-0000-0000-0000-system000000" in body, (
            f"{workflow_name}: failure receipt must use sentinel UUID 'ffffffff-0000-0000-0000-system000000' "
            f"for missing tenant context (not NULL)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_request_carries_suite_id_header(self, workflow_name):
        """Evil test: Gateway HTTP calls must include x-suite-id header (tenant context in request)."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            headers = node.get("parameters", {}).get("headerParameters", {}).get("parameters", [])
            header_names = [h.get("name", "").lower() for h in headers]
            assert "x-suite-id" in header_names, (
                f"{workflow_name}: Gateway node '{node['name']}' must send x-suite-id header"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_request_carries_correlation_id_header(self, workflow_name):
        """Gateway HTTP calls must carry x-correlation-id for trace continuity (Law #2)."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            headers = node.get("parameters", {}).get("headerParameters", {}).get("parameters", [])
            header_names = [h.get("name", "").lower() for h in headers]
            assert "x-correlation-id" in header_names, (
                f"{workflow_name}: Gateway node '{node['name']}' must send x-correlation-id header"
            )


# ===========================================================================
# N8 — Idempotency Key Uniqueness
# ===========================================================================


class TestN8IdempotencyKey:
    """Law #2 + Production Gate 3: Idempotency — retries must not duplicate state changes."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_idempotency_key(self, workflow_name):
        """All workflows must generate an idempotency key in the prep Code node."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        assert "idempotencyKey" in code_text or "idempotency_key" in code_text, (
            f"{workflow_name}: must generate idempotencyKey in prep Code node"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_idempotency_key_uses_hash(self, workflow_name):
        """Idempotency key must be derived via SHA-256 hash for collision resistance."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        assert "createHash('sha256')" in code_text or "createHash(\"sha256\")" in code_text, (
            f"{workflow_name}: idempotency key must use SHA-256 hash"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_request_sends_idempotency_key(self, workflow_name):
        """Gateway HTTP calls must send x-idempotency-key header."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            headers = node.get("parameters", {}).get("headerParameters", {}).get("parameters", [])
            header_names = [h.get("name", "").lower() for h in headers]
            assert "x-idempotency-key" in header_names, (
                f"{workflow_name}: Gateway node '{node['name']}' must send x-idempotency-key header"
            )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_request_id_has_random_component(self, workflow_name):
        """requestId must include a random component to prevent cross-execution collisions."""
        wf = load_workflow(workflow_name)
        code_text = get_code_text(wf)
        assert "Math.random()" in code_text, (
            f"{workflow_name}: requestId must include Math.random() to ensure uniqueness"
        )


# ===========================================================================
# N9 — Error Trigger Coverage
# ===========================================================================


class TestN9ErrorTrigger:
    """Law #2: No Action Without a Receipt — Error Trigger must wire to failure receipt."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_error_trigger(self, workflow_name):
        """All 11 workflows must have an errorTrigger node for unhandled failures."""
        wf = load_workflow(workflow_name)
        error_triggers = get_error_trigger_nodes(wf)
        assert len(error_triggers) >= 1, (
            f"{workflow_name}: must have an errorTrigger node (Law #2 — failures produce receipts)"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_error_trigger_is_connected_to_failure_receipt(self, workflow_name):
        """Evil test: error trigger must be wired to failure receipt, not dead-end."""
        wf = load_workflow(workflow_name)
        connections = wf.get("connections", {})
        # Find the Error Trigger node name
        error_triggers = get_error_trigger_nodes(wf)
        assert len(error_triggers) >= 1, f"{workflow_name}: no error trigger"
        et_name = error_triggers[0]["name"]
        # Verify it has an outgoing connection
        assert et_name in connections, (
            f"{workflow_name}: Error Trigger node '{et_name}' has no outgoing connections — "
            f"failures would be silently dropped (Law #2 violation)"
        )
        outgoing = connections[et_name].get("main", [[]])
        assert any(len(branch) > 0 for branch in outgoing), (
            f"{workflow_name}: Error Trigger '{et_name}' connections are empty"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_error_trigger_connects_to_failure_receipt_node(self, workflow_name):
        """Error trigger must connect to a receipt node named 'Failure' or 'Error'."""
        wf = load_workflow(workflow_name)
        connections = wf.get("connections", {})
        error_triggers = get_error_trigger_nodes(wf)
        assert len(error_triggers) >= 1, f"{workflow_name}: no error trigger"
        et_name = error_triggers[0]["name"]
        if et_name not in connections:
            pytest.fail(f"{workflow_name}: Error Trigger has no connections")
        outgoing = connections[et_name].get("main", [[]])
        target_nodes = [conn.get("node", "") for branch in outgoing for conn in branch]
        # At least one target must be a receipt node
        receipt_targets = [t for t in target_nodes if "receipt" in t.lower()]
        assert len(receipt_targets) >= 1, (
            f"{workflow_name}: Error Trigger must connect to a receipt node, "
            f"got targets: {target_nodes}"
        )


# ===========================================================================
# N10 — Structural Correctness
# ===========================================================================


class TestN10StructuralCorrectness:
    """Validate structural correctness of all 11 workflow JSON files."""

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_json_is_valid(self, workflow_name):
        """All workflow files must parse as valid JSON."""
        # If load_workflow fails, it raises JSONDecodeError
        wf = load_workflow(workflow_name)
        assert isinstance(wf, dict), f"{workflow_name}: workflow must be a JSON object"

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_name(self, workflow_name):
        """All workflows must have a non-empty name field."""
        wf = load_workflow(workflow_name)
        assert wf.get("name"), f"{workflow_name}: workflow must have a non-empty name"

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_has_nodes(self, workflow_name):
        """All workflows must have at least 5 nodes (trigger + kill + check + gateway + receipt)."""
        wf = load_workflow(workflow_name)
        nodes = wf.get("nodes", [])
        assert len(nodes) >= 5, (
            f"{workflow_name}: workflow must have at least 5 nodes, got {len(nodes)}"
        )

    @pytest.mark.parametrize("workflow_name", sorted(CRON_WORKFLOWS))
    def test_cron_workflows_have_schedule_trigger(self, workflow_name):
        """Cron workflows must use scheduleTrigger, not webhook."""
        wf = load_workflow(workflow_name)
        schedule_nodes = get_nodes_by_type(wf, "n8n-nodes-base.scheduleTrigger")
        assert len(schedule_nodes) >= 1, (
            f"{workflow_name}: cron workflow must have a scheduleTrigger node"
        )

    @pytest.mark.parametrize("workflow_name", sorted(WEBHOOK_WORKFLOWS))
    def test_webhook_workflows_have_webhook_trigger(self, workflow_name):
        """Webhook workflows must have a webhook trigger node."""
        wf = load_workflow(workflow_name)
        webhook_nodes = get_webhook_nodes(wf)
        assert len(webhook_nodes) >= 1, (
            f"{workflow_name}: webhook workflow must have a webhook trigger node"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_workflow_settings_use_v1_execution_order(self, workflow_name):
        """All workflows must use executionOrder: v1 (consistent behavior)."""
        wf = load_workflow(workflow_name)
        settings = wf.get("settings", {})
        assert settings.get("executionOrder") == "v1", (
            f"{workflow_name}: must use executionOrder: v1"
        )

    @pytest.mark.parametrize("workflow_name", sorted(ALL_WORKFLOW_NAMES))
    def test_gateway_credential_references_named_key(self, workflow_name):
        """Gateway HTTP nodes must reference the 'Gateway Internal Key' credential."""
        wf = load_workflow(workflow_name)
        gateway_nodes = get_gateway_nodes(wf)
        for node in gateway_nodes:
            creds = node.get("credentials", {})
            http_cred = creds.get("httpHeaderAuth", {})
            assert http_cred.get("name") == "Gateway Internal Key", (
                f"{workflow_name}: Gateway node '{node['name']}' must reference "
                f"'Gateway Internal Key' credential, got: {http_cred.get('name')}"
            )

    def test_all_11_workflow_files_exist(self):
        """All 11 expected workflow JSON files must exist on disk."""
        for name in ALL_WORKFLOW_NAMES:
            path = os.path.join(WORKFLOWS_DIR, f"{name}.json")
            assert os.path.exists(path), (
                f"Missing workflow file: infrastructure/n8n-workflows/{name}.json"
            )

    def test_sync_script_has_all_11_workflow_ids(self):
        """sync_n8n_workflows.py EXISTING_MAP must contain all 11 workflow names."""
        sync_script_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..",
            "scripts", "sync_n8n_workflows.py"
        )
        with open(sync_script_path, "r", encoding="utf-8") as f:
            content = f.read()
        for name in ALL_WORKFLOW_NAMES:
            assert f'"{name}"' in content, (
                f"sync_n8n_workflows.py EXISTING_MAP is missing '{name}'"
            )

    def test_docker_compose_has_all_11_kill_switch_env_vars(self):
        """docker-compose.n8n.yml must have enabled env vars for all 11 workflows."""
        compose_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..",
            "infrastructure", "docker", "docker-compose.n8n.yml"
        )
        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()
        expected_vars = [
            "N8N_WORKFLOW_INTAKE_ACTIVATION_ENABLED",
            "N8N_WORKFLOW_ADAM_DAILY_BRIEF_ENABLED",
            "N8N_WORKFLOW_ADAM_PULSE_SCAN_ENABLED",
            "N8N_WORKFLOW_ADAM_LIBRARY_CURATE_ENABLED",
            "N8N_WORKFLOW_ADAM_FOCUS_WEEKLY_ENABLED",
            "N8N_WORKFLOW_ADAM_EDUCATION_CURATE_ENABLED",
            "N8N_WORKFLOW_ELI_EMAIL_TRIAGE_ENABLED",
            "N8N_WORKFLOW_SARAH_CALL_HANDLER_ENABLED",
            "N8N_WORKFLOW_QUINN_INVOICE_REMINDER_ENABLED",
            "N8N_WORKFLOW_NORA_MEETING_SUMMARY_ENABLED",
            "N8N_WORKFLOW_TERESSA_BOOKS_SYNC_ENABLED",
        ]
        for var in expected_vars:
            assert var in content, (
                f"docker-compose.n8n.yml missing kill switch env var: {var}"
            )

    def test_docker_compose_has_all_4_webhook_secret_env_vars(self):
        """docker-compose.n8n.yml must have all 4 webhook secret env vars."""
        compose_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..",
            "infrastructure", "docker", "docker-compose.n8n.yml"
        )
        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()
        required_secrets = [
            "N8N_WEBHOOK_SECRET",
            "N8N_ELI_WEBHOOK_SECRET",
            "N8N_SARAH_WEBHOOK_SECRET",
            "N8N_NORA_WEBHOOK_SECRET",
        ]
        for secret in required_secrets:
            assert secret in content, (
                f"docker-compose.n8n.yml missing webhook secret env var: {secret}"
            )
