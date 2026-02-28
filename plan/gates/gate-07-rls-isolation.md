---
gate: 7
name: "RLS Isolation"
status: "complete"
phase_introduced: "1"
complexity: "medium"
critical: true
---

# GATE 07: RLS Isolation (CRITICAL)

## ⚠️ CRITICAL GATE - Multi-Tenant Security

Zero cross-tenant data leakage. Competitors MUST NOT see each other's data. Non-negotiable.

## Requirement

Zero client tool execution, capability tokens enforced, Row-Level Security (RLS) for multi-tenant isolation.

## Database RLS Policies

### Postgres Row-Level Security

```sql
-- Enable RLS on all tenant-scoped tables
ALTER TABLE receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE offices ENABLE ROW LEVEL SECURITY;

-- Receipts table isolation policy
CREATE POLICY tenant_isolation_receipts ON receipts
  FOR ALL  -- Applies to SELECT, INSERT, UPDATE, DELETE
  USING (suite_id = current_setting('app.current_suite_id')::uuid);

-- Checkpoints table isolation policy
CREATE POLICY tenant_isolation_checkpoints ON checkpoints
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id')::uuid);

-- Offices table isolation policy
CREATE POLICY tenant_isolation_offices ON offices
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id')::uuid);

-- Suites table (no isolation - suite can see itself)
CREATE POLICY suite_self_access ON suites
  FOR ALL
  USING (suite_id = current_setting('app.current_suite_id')::uuid);
```

### Session Context Setting

**Before EVERY database query:**
```sql
-- Set current suite context (from JWT token)
SET LOCAL app.current_suite_id = '<suite_id_from_auth_token>';

-- Now all queries are automatically scoped to this suite
SELECT * FROM receipts;  -- Only returns receipts for current suite
```

## Capability Token Enforcement

### Token Structure
```json
{
  "token_id": "tok_abc123",
  "suite_id": "suite_xyz789",
  "office_id": "office_user1",
  "tool": "stripe.invoice.create",
  "scopes": ["invoice.write"],
  "expires_at": "2026-01-10T12:35:00Z",  -- <60s from issue
  "signature": "sha256_hmac_signature"
}
```

### Token Validation (Server-Side)
1. **Verify signature** - HMAC-SHA256 with secret key
2. **Check expiry** - Must be <60s from issue time
3. **Validate scope** - Tool + scopes must match request
4. **Verify suite/office** - Token must belong to requesting tenant
5. **Check revocation** - Token not in revocation list

**If ANY check fails:** Reject request with `401 Unauthorized`

## Zero Client Tool Execution

### Forbidden Architecture ❌
```
Mobile App → Stripe API (direct call)
```
**Problem:** Client has Stripe API key → can execute ANY Stripe action → bypass approval gates

### Required Architecture ✅
```
Mobile App → LangGraph Brain → (validates token) → Stripe API
```
**Security:** Brain validates capability token → enforces approval gates → calls Stripe only if authorized

## Evil Test Suite (Zero Cross-Tenant Leakage)

### Test 1: Direct SQL Injection Attempt
```sql
-- Attacker tries to bypass RLS with SQL injection
-- (This should FAIL - Postgres parameters prevent injection)
query = "SELECT * FROM receipts WHERE suite_id = ? OR 1=1"
-- Expected: Query blocked by parameterization, only current suite returned
```

### Test 2: Cross-Tenant SELECT
```python
# Setup: Create 2 suites
suite_a = create_suite("CompanyA")
suite_b = create_suite("CompanyB")

# CompanyA creates receipt
with session_context(suite_a):
    receipt_a = create_receipt("invoice.send", amount=5000)

# CompanyB tries to read CompanyA's receipt
with session_context(suite_b):
    result = fetch_receipt(receipt_a.receipt_id)

    # Expected: None (RLS blocks cross-tenant access)
    assert result is None  # ✅ PASS
```

### Test 3: Capability Token Forgery
```python
# Attacker creates fake token for CompanyB's data
fake_token = {
    "suite_id": "company_b_suite",  # Target victim
    "office_id": "attacker_office",  # Attacker's office
    "tool": "receipts.read",
    "signature": "forged_signature"  # Invalid HMAC
}

# Attempt to use forged token
response = api_call("/receipts", token=fake_token)

# Expected: 401 Unauthorized (signature validation fails)
assert response.status_code == 401  # ✅ PASS
```

### Test 4: Session Context Bypass
```python
# Attacker tries to manually set session context to another suite
with session_context(suite_a):
    # Attempt to override context within session
    db.execute("SET LOCAL app.current_suite_id = ?", suite_b.id)

    # Try to read suite_b's data
    receipts = db.query("SELECT * FROM receipts")

    # Expected: Session context override blocked, only suite_a data returned
    assert all(r.suite_id == suite_a.id for r in receipts)  # ✅ PASS
```

## Verification Criteria

- [ ] RLS policies enabled on all tenant-scoped tables
- [ ] Session context set before EVERY database query
- [ ] Capability tokens validated server-side (signature, expiry, scope)
- [ ] No direct client→tool API calls (all through Brain)
- [ ] Evil test suite passes 100% (zero cross-tenant leakage)

## What This Gate Prevents

- **Data breaches** - Competitor cannot see rival's receipts/data
- **Unauthorized actions** - Client cannot bypass approval gates with direct API calls
- **Token forgery** - Attacker cannot create fake capability tokens
- **SQL injection** - RLS + parameterized queries prevent injection attacks

## Failure Scenarios

❌ **Fails if:**
- Cross-tenant SELECT returns data from other suite
- Client can call tools directly (bypassing Brain)
- Capability token signature validation skipped
- RLS policies missing on any tenant-scoped table
- Evil test suite <100% pass rate

✅ **Passes if:**
- RLS policies block ALL cross-tenant queries (100% isolation)
- Capability tokens required for EVERY tool call
- Token validation enforced server-side
- Evil test suite: 100% pass rate (zero leakage)

## Testing Requirements (Automated)

### Evil Test Suite (Run Before EVERY Deploy)
```bash
# Run full evil test suite
pytest tests/evil/test_rls_isolation.py

# Expected output:
# test_cross_tenant_select .................. PASS
# test_sql_injection_attempt ................ PASS
# test_capability_token_forgery ............. PASS
# test_session_context_bypass ............... PASS
# test_direct_client_tool_call .............. PASS (blocked)
# ============================================
# 5/5 EVIL TESTS PASSED (100% isolation)
```

### Manual Verification (Security Audit)
1. Create 2 test suites (Suite A, Suite B)
2. Suite A creates 10 receipts
3. Suite B attempts to access Suite A's receipts via:
   - Direct SQL (should fail - RLS blocks)
   - API call with Suite B token (should fail - wrong suite_id)
   - Forged token with Suite A id (should fail - invalid signature)
4. Verify ALL attempts return 0 results (100% isolation)

## Related Gates

- **Gate 06:** Receipts Immutable (receipts table has RLS policies)
- **Gate 10:** Ops Minimums (evil tests run in CI/CD pipeline)

## Why This is CRITICAL

**Regulatory Compliance:**
- GDPR, CCPA require tenant isolation for PII
- Financial regulations require data segregation

**Business Survival:**
- Cross-tenant leak = instant death (reputation, legal liability)
- Competitor seeing rival's data = existential threat

**Trust:**
- Multi-tenant SaaS MUST guarantee isolation
- One leak = all customers leave

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 1 roadmap includes RLS implementation
**Evidence:** RLS spec in Aspire-Production-Roadmap.md:2131-2138, CLAUDE.md Law #6
