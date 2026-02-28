# Limits Policy — Plaid Transfer

## Initial Limits (Conservative)

| Limit Type | Initial Cap | Notes |
|-----------|------------|-------|
| Per transfer | $5,000 | Maximum single transfer amount |
| Per day (per office) | $10,000 | Aggregate daily limit per office |
| Per customer/week | $25,000 | Rolling 7-day window per suite |

## Ramp Conditions

Limits can be increased when ALL of the following are met:
- After **N successful transfers** (minimum 10) with no returns in the period.
- After **M days without returns** (minimum 30 days).
- **Manual review required** for all limit increases — automated increases are not permitted.
- Limit increase request goes through Ava orchestrator as a YELLOW-tier operation (requires user confirmation).

## Limit Tiers

| Tier | Per Transfer | Per Day | Per Week | Criteria |
|------|-------------|---------|----------|----------|
| Starter | $5,000 | $10,000 | $25,000 | Initial (all new suites) |
| Standard | $15,000 | $30,000 | $75,000 | 10+ successful, 30 days no returns |
| Business | $50,000 | $100,000 | $250,000 | 50+ successful, 90 days no returns, manual review |

## Policy Enforcement
- Limits are enforced by the policy engine (`backend/orchestrator/services/policy_engine.py`).
- Transfer requests exceeding limits are denied with receipt: `outcome: denied`, `reason_code: limit_exceeded` (Law #3).
- Limit downgrades (due to returns/fraud) are immediate and automatic.
- Limit upgrades require manual review and generate a privileged audit receipt.

## Audit Trail
- All limit changes (up or down) are stored in the privileged audit log with receipts (Law #2).
- Receipt includes: old limit, new limit, reason for change, who approved (if manual upgrade).
- Limit history is immutable — no deletion or modification of limit change records.
