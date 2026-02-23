# Ava Admin — Fewshots (ops patterns)

## 1) Incident triage (no tenant data)
```json
{
  "status": "ok",
  "outputs": {
    "correlation_id": "admin_501",
    "route": { "skillpack_id": "ops_incident", "confidence": 0.84, "reason_codes": ["incident.triage","ops.audit"] },
    "risk": { "tier": "medium", "required_presence": "none" },
    "governance": {
      "approval_required": false,
      "approval_reason_codes": [],
      "policy_version": "approval_gates_v2_2026-02-12",
      "tool_policy_version": "tool_policy_v2_2026-02-12",
      "requested_tools": ["supabase","trustspine.receipts.query"],
      "allowed_tools": ["supabase","trustspine.receipts.query"],
      "payload_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    },
    "plan": {
      "steps": [
        "Pull last 50 denial receipts by reason_code",
        "Correlate by service + deploy version",
        "Produce incident summary + suspected regression window",
        "Recommend mitigations and rollback trigger"
      ],
      "blockers": []
    },
    "notes": "Do not fetch tenant payloads. Use counts/aggregates unless explicitly authorized."
  }
}
```

## 2) Policy change without change ticket → fail-closed
```json
{
  "status": "fatal_error",
  "outputs": {
    "correlation_id": "admin_900",
    "route": { "skillpack_id": "ops_policy", "confidence": 0.72, "reason_codes": ["policy.change","missing_context"] },
    "risk": { "tier": "red", "required_presence": "ava_video" },
    "governance": {
      "approval_required": true,
      "approval_reason_codes": ["platform.policy.update"],
      "policy_version": "approval_gates_v2_2026-02-12",
      "tool_policy_version": "tool_policy_v2_2026-02-12",
      "requested_tools": ["policy.evaluate","trustspine.request_approval"],
      "allowed_tools": ["policy.evaluate"],
      "payload_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    },
    "plan": {
      "steps": [
        "BLOCKER: Need change ticket / authorization context",
        "Draft policy diff and rollback plan",
        "Request approval with payload hash binding"
      ],
      "blockers": ["missing_change_ticket","approval_required","presence_required"]
    }
  },
  "error": {
    "code": "missing_context",
    "message": "Policy changes require a change ticket/authorization context, plus approval and Ava Video presence."
  }
}
```
