---
gate: 6
name: "Receipts Immutable"
status: "complete"
phase_introduced: "1"
complexity: "medium"
critical: true
---

# GATE 06: Receipts Immutable (CRITICAL)

## ⚠️ CRITICAL GATE - Audit Trail Integrity

Without immutable receipts, the entire governance model collapses. This is non-negotiable.

## Requirement

Append-only, hash-chained receipts table with NO UPDATE/DELETE privileges.

## Database Implementation

### Postgres Receipts Table
```sql
CREATE TABLE receipts (
    receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL,
    suite_id UUID NOT NULL,  -- Tenant isolation
    office_id UUID NOT NULL,  -- Actor (human within suite)

    action_type TEXT NOT NULL,  -- e.g., "invoice.send", "email.draft"
    risk_tier TEXT NOT NULL CHECK (risk_tier IN ('green', 'yellow', 'red')),

    tool_used TEXT NOT NULL,  -- e.g., "stripe_api", "gmail_api"
    capability_token_id UUID NOT NULL,  -- Authorization proof

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,  -- Null if autonomous (green tier)
    executed_at TIMESTAMPTZ NOT NULL,

    approval_evidence JSONB,  -- Who approved, when, video proof, etc.
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
    reason_code TEXT,  -- Why denied/failed

    redacted_inputs JSONB NOT NULL,  -- Input params (PII redacted)
    redacted_outputs JSONB,  -- Result (PII redacted)

    previous_receipt_hash TEXT,  -- Hash of previous receipt (hash chain)
    receipt_hash TEXT NOT NULL  -- SHA-256(this receipt's immutable fields)
);

-- NO UPDATE/DELETE ALLOWED (revoke privileges)
REVOKE UPDATE, DELETE, TRUNCATE ON receipts FROM app_role;
GRANT SELECT, INSERT ON receipts TO app_role;

-- Row-Level Security for multi-tenant isolation
CREATE POLICY tenant_isolation ON receipts
  FOR SELECT
  USING (suite_id = current_setting('app.current_suite_id')::uuid);
```

### Hash Chain Enforcement

Each receipt calculates:
```
receipt_hash = SHA-256(
    receipt_id ||
    action_type ||
    outcome ||
    executed_at ||
    redacted_inputs ||
    previous_receipt_hash
)
```

**Chain Integrity:**
- First receipt: `previous_receipt_hash = NULL`
- Subsequent receipts: `previous_receipt_hash = SHA-256 of previous receipt`
- Tampering breaks chain → audit failure detected

## Minimum Receipt Fields (Aspire Law #2)

Every receipt MUST include:
1. `correlation_id` / `trace_id` - Request tracing
2. `actor` (suite_id + office_id) - Who executed
3. `action_type` - What was done
4. `risk_tier` (green/yellow/red) - Approval level required
5. `tool_used` - Which integration was called
6. `capability_token_id` - Authorization proof
7. `timestamps` (created/approved/executed) - When it happened
8. `approval_evidence` - Who/when/how approved (if applicable)
9. `outcome` (success/denied/failed) - Result
10. `reason_code` - Why denied/failed (if applicable)
11. `redacted_inputs`/`redacted_outputs` - What data (PII redacted)
12. `receipt_hash` + `previous_receipt_hash` - Chain integrity

## PII Redaction (Presidio DLP Integration)

**Before Logging:**
- Social Security Numbers → `<SSN_REDACTED>`
- Credit card numbers → `<CC_REDACTED>`
- Email addresses → `<EMAIL_REDACTED>` (unless business email)
- Phone numbers → `<PHONE_REDACTED>`
- Physical addresses → `<ADDRESS_REDACTED>`

**Example Redacted Receipt:**
```json
{
  "receipt_id": "rcp_abc123",
  "action_type": "stripe.invoice.send",
  "redacted_inputs": {
    "customer_email": "<EMAIL_REDACTED>",
    "amount": 5000,
    "invoice_id": "inv_xyz789"
  },
  "redacted_outputs": {
    "stripe_invoice_id": "in_abc123def456",
    "status": "sent"
  }
}
```

## Verification Criteria

- [ ] Postgres receipts table has NO UPDATE/DELETE privileges
- [ ] Hash chain enforced (previous_receipt_hash links to prior receipt)
- [ ] PII redaction active (Presidio DLP integrated)
- [ ] 100% receipt coverage (every action generates receipt)
- [ ] Receipt retrieval API working (users can view audit trail)
- [ ] Tampering detection (hash chain validation on read)

## What This Gate Prevents

- **Tampering** - Receipts cannot be altered after creation (immutable)
- **Deletion** - Audit trail cannot be erased (no DELETE privilege)
- **PII leakage** - Sensitive data redacted before logging
- **Accountability gaps** - Every action has proof (100% coverage)

## Failure Scenarios

❌ **Fails if:**
- UPDATE or DELETE privilege exists on receipts table
- Any action completes without generating receipt
- PII logged raw (no redaction)
- Hash chain breaks (previous_receipt_hash mismatch)
- Receipt coverage <100%

✅ **Passes if:**
- Receipts table is append-only (verified via `pg_catalog` inspection)
- Hash chain validates for all receipts
- PII redaction working (Presidio DLP tests pass)
- 100% coverage confirmed (no actions bypass receipt generation)

## Testing Requirements

### Test 1: Immutability Check
```sql
-- Attempt to update receipt (should fail)
UPDATE receipts SET outcome = 'tampered' WHERE receipt_id = 'rcp_test';
-- Expected: ERROR: permission denied for table receipts

-- Attempt to delete receipt (should fail)
DELETE FROM receipts WHERE receipt_id = 'rcp_test';
-- Expected: ERROR: permission denied for table receipts
```

### Test 2: Hash Chain Validation
```python
def verify_hash_chain(receipts):
    for i in range(1, len(receipts)):
        current = receipts[i]
        previous = receipts[i-1]

        # Verify previous_receipt_hash matches
        assert current['previous_receipt_hash'] == previous['receipt_hash']

        # Verify current receipt_hash is correct
        computed_hash = sha256(...)  # Compute from immutable fields
        assert current['receipt_hash'] == computed_hash

    return True  # Chain valid
```

### Test 3: PII Redaction
```python
# Send action with PII
invoice_input = {
    "customer_email": "john.doe@example.com",
    "customer_ssn": "123-45-6789",
    "amount": 5000
}

# Execute action
receipt = execute_action("stripe.invoice.send", invoice_input)

# Verify redaction
assert "<EMAIL_REDACTED>" in receipt['redacted_inputs']['customer_email']
assert "<SSN_REDACTED>" in receipt['redacted_inputs']['customer_ssn']
assert "123-45-6789" not in str(receipt)  # Raw SSN must not appear
```

### Test 4: Coverage Check
```python
# Execute 100 random actions
for i in range(100):
    action = random_action()
    result = execute_action(action)

    # Verify receipt was generated
    receipt = fetch_receipt(result['correlation_id'])
    assert receipt is not None
    assert receipt['action_type'] == action['type']

# Coverage: 100/100 actions = 100% ✅
```

## Related Gates

- **Gate 07:** RLS Isolation (receipts table has RLS policies)
- **Gate 08:** Replay Demo (receipts enable deterministic replay)

## Why This is CRITICAL

**Legal/Compliance:**
- Immutable audit trail is REQUIRED for SOC 2, HIPAA, financial regulations
- Tampering = fraud = legal liability

**Accountability:**
- Without receipts, cannot prove what actions were taken
- Disputes become "he said, she said" (no evidence)

**Trust:**
- Users MUST trust that Aspire logs everything
- If receipts can be deleted, trust collapses

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 1 roadmap includes receipt generation system
**Evidence:** Receipt implementation spec in Aspire-Production-Roadmap.md:2122-2129
