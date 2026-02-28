# Kill Switch Runbook

## Execution Control Modes

| Mode | Behavior | When to Use |
|------|----------|------------|
| `ENABLED` | Normal operation. GREEN auto-approved, YELLOW requires confirmation, RED requires authority. | Default mode. |
| `APPROVAL_ONLY` | All operations require manual admin approval, regardless of risk tier. | Suspected issues, elevated risk, pilot phase, after incidents. |
| `DISABLED` | All operations for the affected provider/tenant are blocked. Fail closed. | Suspected money/payroll risk, security breach, fraud detection. |

## How to Activate

### Via Admin API
```
POST /admin/kill-switch
Authorization: Bearer <JWT>
Content-Type: application/json

{
  "suite_id": "<uuid>",
  "provider": "gusto|plaid|stripe|moov",
  "mode": "APPROVAL_ONLY|DISABLED",
  "reason": "Description of why the switch was activated"
}
```

### Scope Options
- **Per-provider, per-tenant**: Affects one provider for one suite (most common).
- **Per-provider, all tenants**: Affects one provider across all suites (provider-wide outage).
- **All providers, per-tenant**: Affects all providers for one suite (tenant-level investigation).

## Important Rules
- **Always record a privileged audit entry and receipt** when changing execution controls (Law #2).
- Use `DISABLED` for suspected money/payroll risk — never `APPROVAL_ONLY` for financial security incidents.
- Restoring to `ENABLED` also requires a receipt and should be done only after the root cause is identified and resolved.
- Kill switch state is stored in the policy engine (`backend/orchestrator/services/policy_engine.py`).
- The LangGraph orchestrator checks execution controls before every tool execution (Law #1).

## Recovery Procedure
1. Identify and resolve the root cause.
2. Run verification tests in simulation mode (GREEN-tier read-only checks).
3. Restore to `ENABLED` via Admin API.
4. Monitor closely for 24 hours after restoration.
5. Generate a receipt for the restoration event.
