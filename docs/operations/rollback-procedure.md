# Aspire Orchestrator — Rollback Procedure

## Pre-Rollback Checklist

- [ ] Identify the commit that introduced the regression
- [ ] Verify current test suite status
- [ ] Notify team of rollback intent
- [ ] Confirm receipt chain integrity before rollback

## Git-Based Rollback

### Option A: Revert a Specific Commit

```bash
# Identify the bad commit
git log --oneline -10

# Revert it (creates a new commit — safe, auditable)
git revert <commit-sha>

# Verify tests pass
cd backend/orchestrator
python -m pytest tests/ -v --tb=short

# Push revert
git push origin main
```

### Option B: Reset to Known Good State

```bash
# Only if revert is insufficient — destructive, requires coordination
git log --oneline -20  # Find last known good commit

# Create a rollback branch for safety
git checkout -b rollback/<date>
git reset --hard <known-good-sha>

# Verify
python -m pytest tests/ -v --tb=short

# Merge back via PR (never force-push main)
```

## Database Rollback

### Receipts (Append-Only — NO Rollback Needed)

Receipts are **append-only** (Law #2). There is no UPDATE or DELETE on the receipts table. A code rollback does not require receipt table changes.

If a bug caused incorrect receipts to be written:
1. Do NOT delete them (immutability)
2. Write a **correction receipt** with `receipt_type=correction` and reference the original `receipt_id`
3. The receipt chain verifier (`verify_chain`) will flag the correction

### In-Memory State (Phase 1)

Phase 1 uses in-memory stores (receipt_store.py, a2a_service.py). A process restart clears all in-memory state. This is acceptable for Phase 1; Phase 2 adds Supabase persistence.

## Configuration Rollback

### Environment Variables

```bash
# Save current config
env | grep ASPIRE_ > /tmp/aspire-env-backup.txt

# Restore previous values
export ASPIRE_TOKEN_SIGNING_KEY="<previous-key>"
export ASPIRE_CORS_ORIGINS="<previous-origins>"

# Restart
pkill -f "uvicorn.*aspire_orchestrator" || true
cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator
python -m uvicorn aspire_orchestrator.server:app --host 0.0.0.0 --port 8000
```

### CORS Origins

If CORS was changed and breaks clients:
```bash
export ASPIRE_CORS_ORIGINS="http://localhost:3100,http://127.0.0.1:3100"
```

### Signing Key Rotation

If the signing key was rotated and breaks in-flight tokens:
1. Set the new key
2. Restart orchestrator
3. All in-flight tokens will be rejected (TTL < 60s, so natural expiry within 1 minute)
4. New tokens mint with the new key

## Feature Flag Kill Switch

For emergency feature disable without code changes:

```bash
# Disable specific features via environment
export ASPIRE_SAFETY_GATE_ENABLED=false    # Bypass safety gate
export ASPIRE_DLP_ENABLED=false            # Bypass PII redaction (emergency only)
```

These should only be used as last-resort emergency measures and must be re-enabled within 1 hour.

## Post-Rollback Verification

```bash
# 1. Health check
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz

# 2. Full test suite
cd /mnt/c/Users/tonio/Projects/myapp
source ~/venvs/aspire/bin/activate
python -m pytest backend/orchestrator/tests/ -v --tb=short

# 3. Receipt chain integrity
curl -X POST http://localhost:8000/v1/receipts/verify-run \
  -H "Content-Type: application/json" \
  -d '{"suite_id": "<your-suite-id>"}'

# 4. Metrics baseline
curl http://localhost:8000/metrics | grep aspire_orchestrator_requests_total
```
