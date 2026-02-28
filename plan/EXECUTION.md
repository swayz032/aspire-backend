# EXECUTION.md -- Phase 0B Deterministic Runbook

> **STATUS: COMPLETE (Cloud: 2026-02-10, Local Dev: 2026-02-12)**
> Execution was performed against Supabase project `qtuehjqlcmfcascqjjhc` via Session Pooler.
> 49 migrations applied, 5 Edge Functions deployed, Desktop integrated, 27/27 RLS tests pass.
> PR #1 merged to swayz032/Aspire-Desktop. www.aspireos.app live.
>
> **Local Dev (Skytech Tower) completed 2026-02-12:**
> WSL2 Ubuntu 22.04 (23GB RAM), Postgres 16.11 + pgvector 0.8.0 (:5432), Redis 7 (:6379),
> CUDA 12.6 (RTX 5060), Python 3.11.14, Node.js v20.20.0, Docker Desktop 29.2.0,
> n8n via Docker (:5678), OTEL+Prometheus+Grafana (:4317/:9090/:3000),
> n8n-mcp + n8n-skills (7) configured, SLI/SLO defined, Git/SSH verified.
>
> Every command is copy-pasteable. Every path is canonical.
> Last Updated: 2026-02-12
> Ecosystem: aspire_ecosystem_v12.7_2026-02-03 (see DEPENDENCIES.lock.md)

---

## Prerequisites

- Supabase CLI installed and authenticated (`supabase login`)
- Supabase project created and linked (`supabase link --project-ref <ref>`)
- Go 1.22+ installed (module requires `go 1.22`, dependency: `pgx/v5`)
- Node.js 18+ / Deno (for Supabase Edge Functions)
- `psql` access to Supabase database (direct connection string)
- k6 installed (optional, for stress tests -- https://k6.io/docs/get-started/installation/)

---

## Variable Definitions

Set these before running any commands. All subsequent steps reference them.

```bash
# --- REQUIRED: Set these to your actual values ---
ECOSYSTEM_ROOT="plan/temp_ecosystem_scan/aspire_ecosystem_v12.7_2026-02-03"
SUPABASE_PROJECT_REF="your-project-ref"
SUPABASE_DB_URL="postgresql://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres"

# --- Derived paths (do not edit) ---
MIGRATIONS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/migrations"
EDGE_FUNCTIONS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/04_EDGE_FUNCTIONS/supabase/functions"
E2E_TESTS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql"
STRESS_TESTS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS"
GO_VERIFIER_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/01_ORIGINAL_INPUTS/claude_handoff_4_0/phase0_bootstrap/aspire_claude_bootstrap"
A2A_ADDON_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/A2A_INBOX_V6"
A2A_MIGRATIONS_DIR="${A2A_ADDON_DIR}/02_DB/migrations"
BOOTSTRAP_SCRIPTS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/CLAUDE_BOOTSTRAP/scripts"
BOOTSTRAP_TESTS_DIR="${ECOSYSTEM_ROOT}/platform/trust-spine/CLAUDE_BOOTSTRAP/tests"
```

---

## Step 0: Verify Folder Structure

Run this block to confirm all required ecosystem paths exist before proceeding.
If any line prints `MISSING`, stop and resolve before continuing.

```bash
echo "=== Verifying Phase 0B ecosystem paths ==="

for DIR in \
  "${MIGRATIONS_DIR}" \
  "${EDGE_FUNCTIONS_DIR}/approval-events" \
  "${EDGE_FUNCTIONS_DIR}/inbox" \
  "${EDGE_FUNCTIONS_DIR}/outbox-executor" \
  "${EDGE_FUNCTIONS_DIR}/outbox-worker" \
  "${EDGE_FUNCTIONS_DIR}/policy-eval" \
  "${EDGE_FUNCTIONS_DIR}/_shared" \
  "${E2E_TESTS_DIR}" \
  "${STRESS_TESTS_DIR}/k6" \
  "${STRESS_TESTS_DIR}/pgbench" \
  "${GO_VERIFIER_DIR}/internal/receiptsverifier" \
  "${A2A_MIGRATIONS_DIR}" \
; do
  if [ -d "$DIR" ]; then
    echo "  OK   $DIR"
  else
    echo "  MISSING  $DIR"
  fi
done

echo ""
echo "=== Verifying migration file count ==="
CORE_COUNT=$(ls "${MIGRATIONS_DIR}"/*.sql 2>/dev/null | wc -l)
A2A_COUNT=$(ls "${A2A_MIGRATIONS_DIR}"/*.sql 2>/dev/null | wc -l)
echo "  Core migrations: ${CORE_COUNT} (expected: 42)"
echo "  A2A migrations:  ${A2A_COUNT}  (expected: 7)"

echo ""
echo "=== Verifying Go module ==="
if [ -f "${GO_VERIFIER_DIR}/go.mod" ]; then
  echo "  OK   go.mod found"
  head -3 "${GO_VERIFIER_DIR}/go.mod"
else
  echo "  MISSING  go.mod"
fi

echo ""
echo "=== Structure verification complete ==="
```

---

## Step 1: Apply Trust Spine Core Migrations (42 files)

Authority document: `${ECOSYSTEM_ROOT}/platform/trust-spine/03_SUPABASE_MIGRATIONS_ADDON/MIGRATION_ORDER_ADDON.md`

Migrations MUST be applied in lexicographic filename order (Supabase default).
The 42 files are organized in 6 groups:

### Group 1: Prerequisites -- Tenancy + Approvals + Provider Call Log (11 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105000100_tenancy_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105000200_tenancy_helpers.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105000300_tenancy_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105000400_tenancy_triggers.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105002000_approvals_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105002100_approvals_triggers.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105002200_approvals_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105002300_approvals_rpcs.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105006000_provider_call_log_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105006100_provider_call_log_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260105006200_provider_call_log_rpcs.sql"
```

### Group 2: Suite/Office Identity Bridge (2 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106006000_suite_office_identity.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106006100_suite_tenant_sync.sql"
```

### Group 3: Inbox/Outbox + Approval Events (7 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007000_inbox_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007100_inbox_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007200_outbox_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007300_outbox_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007400_outbox_rpcs.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007500_approval_events_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260106007600_approval_events_rls.sql"
```

### Group 4: Hardening + Scaffolds (9 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008000_trust_immutability.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008100_trust_idempotency.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008200_trust_pii_redaction.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008300_receipts_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008350_receipts_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008400_policy_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008500_executor_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008600_certification_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008700_receipts_crypto.sql"
```

### Group 5: Enterprise Controls (10 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008800_capability_tokens_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008850_capability_tokens_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008900_execution_controls_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116008950_execution_controls_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009000_privileged_audit_log_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009050_privileged_audit_log_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009100_trace_context_columns.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009200_retention_jobs.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009300_release_flags_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260116009400_policy_rls.sql"
```

### Group 6: Ava Video Presence Enforcement (3 files)

```bash
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260201009500_presence_sessions_schema.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260201009550_presence_sessions_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${MIGRATIONS_DIR}/20260201009600_presence_sessions_rpcs.sql"
```

### Batch Alternative (all 42 in lexicographic order)

If you prefer a single command that applies all migrations in order:

```bash
for f in $(ls "${MIGRATIONS_DIR}"/*.sql | sort); do
  echo "Applying: $(basename $f)"
  psql "${SUPABASE_DB_URL}" -f "$f" || { echo "FAILED: $f"; exit 1; }
done
echo "=== All 42 core migrations applied ==="
```

---

## Step 2: Run Bootstrap Smoke Tests

These bootstrap-level tests verify core RLS and redaction before deploying Edge Functions.

```bash
echo "=== RLS cross-tenant isolation (bootstrap) ==="
psql "${SUPABASE_DB_URL}" -f "${BOOTSTRAP_TESTS_DIR}/rls/01_rls_blocks_cross_tenant.sql"

echo "=== Provider log redaction (bootstrap) ==="
psql "${SUPABASE_DB_URL}" -f "${BOOTSTRAP_TESTS_DIR}/redaction/01_provider_log_redaction.sql"

echo "=== Dev seed data ==="
psql "${SUPABASE_DB_URL}" -f "${BOOTSTRAP_SCRIPTS_DIR}/dev_seed.sql"

echo "=== DB smoke test ==="
psql "${SUPABASE_DB_URL}" -f "${BOOTSTRAP_SCRIPTS_DIR}/db_smoketest.sql"
```

---

## Step 3: Deploy Core Edge Functions (5 functions)

Each function has an `index.ts` entry point. The `_shared/` directory contains common utilities
(`auth.ts`, `correlation.ts`, `errors.ts`) imported by all functions.

### 3a: Copy shared utilities into the Supabase project

```bash
# If using supabase CLI, ensure the functions directory structure exists
mkdir -p supabase/functions/_shared
cp "${EDGE_FUNCTIONS_DIR}/_shared/auth.ts" supabase/functions/_shared/
cp "${EDGE_FUNCTIONS_DIR}/_shared/correlation.ts" supabase/functions/_shared/
cp "${EDGE_FUNCTIONS_DIR}/_shared/errors.ts" supabase/functions/_shared/
```

### 3b: Deploy each function

```bash
# 1. approval-events
cp -r "${EDGE_FUNCTIONS_DIR}/approval-events" supabase/functions/
supabase functions deploy approval-events --project-ref "${SUPABASE_PROJECT_REF}"

# 2. inbox
cp -r "${EDGE_FUNCTIONS_DIR}/inbox" supabase/functions/
supabase functions deploy inbox --project-ref "${SUPABASE_PROJECT_REF}"

# 3. outbox-executor
cp -r "${EDGE_FUNCTIONS_DIR}/outbox-executor" supabase/functions/
supabase functions deploy outbox-executor --project-ref "${SUPABASE_PROJECT_REF}"

# 4. outbox-worker
cp -r "${EDGE_FUNCTIONS_DIR}/outbox-worker" supabase/functions/
supabase functions deploy outbox-worker --project-ref "${SUPABASE_PROJECT_REF}"

# 5. policy-eval
cp -r "${EDGE_FUNCTIONS_DIR}/policy-eval" supabase/functions/
supabase functions deploy policy-eval --project-ref "${SUPABASE_PROJECT_REF}"
```

### 3c: Verify deployments

```bash
supabase functions list --project-ref "${SUPABASE_PROJECT_REF}"
# Expected output: 5 functions (approval-events, inbox, outbox-executor, outbox-worker, policy-eval)
```

---

## Step 4: Run E2E Tests (5 numbered test files)

The `13_E2E_TESTS/sql/` directory contains 5 numbered tests plus 3 unnumbered variants.
Run the 5 canonical numbered tests in order:

```bash
echo "=== E2E Test 01: Tenant Isolation ==="
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/01_tenant_isolation.sql"

echo "=== E2E Test 02: Idempotency Replay ==="
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/02_idempotency_replay.sql"

echo "=== E2E Test 03: Outbox Double Claim ==="
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/03_outbox_double_claim.sql"

echo "=== E2E Test 04: Receipt Hash Verify ==="
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/04_receipt_hash_verify.sql"

echo "=== E2E Test 05: Video Presence Enforcement ==="
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/05_video_presence_enforcement.sql"
```

**Pass criteria:** All 5 tests must complete without errors. Any failure is a deployment blocker.

Note: The directory also contains 3 unnumbered test files (`idempotency_replay.sql`,
`outbox_concurrency.sql`, `tenant_isolation.sql`). These appear to be earlier drafts
of the numbered versions. Run them only if you need additional coverage:

```bash
# Optional: unnumbered variants
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/tenant_isolation.sql"
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/idempotency_replay.sql"
psql "${SUPABASE_DB_URL}" -f "${E2E_TESTS_DIR}/outbox_concurrency.sql"
```

---

## Step 5: Build and Deploy Go Receipt Verifier

The Go receipt verifier is located at:
`${GO_VERIFIER_DIR}/internal/receiptsverifier/`

Module: `aspire` (Go 1.22, dependency: `github.com/jackc/pgx/v5 v5.5.5`)

### 5a: Build

```bash
cd "${GO_VERIFIER_DIR}"

# Download dependencies
go mod download

# Run unit tests
go test ./internal/receiptsverifier/... -v

# Build the binary (adjust GOOS/GOARCH for your target platform)
go build -o aspire-receipt-verifier ./internal/receiptsverifier/
```

### 5b: Verify

```bash
# Run the verify receipts script (requires DATABASE_URL)
export DATABASE_URL="${SUPABASE_DB_URL}"
bash "${GO_VERIFIER_DIR}/scripts/14_verify_receipts_signatures.sh"
```

### Go Verifier Source Files

| File | Purpose |
|------|---------|
| `internal/receiptsverifier/verify.go` | Core hash-chain verification logic |
| `internal/receiptsverifier/db.go` | Database access (pgx) |
| `internal/receiptsverifier/http.go` | HTTP handler for verification endpoint |
| `internal/receiptsverifier/payload.go` | Receipt payload serialization |
| `internal/receiptsverifier/verify_run_test.go` | Unit tests |
| `internal/canon/canonical.go` | Canonical JSON serialization |
| `internal/keys/keystore.go` | Key management |
| `internal/keys/types.go` | Key type definitions |

---

## Step 6: Run Stress Tests (OPTIONAL)

Requires: Deployed Supabase project with migrations applied, valid JWT, `WORKER_SECRET`.

### 6a: k6 Load Tests

```bash
# Set environment variables
export SUPABASE_FUNCTIONS_URL="https://${SUPABASE_PROJECT_REF}.functions.supabase.co"
export JWT="your-supabase-jwt"
export WORKER_SECRET="your-worker-secret"

# Test 1: policy-eval (read-heavy)
# Pass criteria: p95 < 300ms at 50 VUs for 60s
k6 run "${STRESS_TESTS_DIR}/k6/policy_eval.js"

# Test 2: approval-events (write-heavy)
# Pass criteria: error rate < 0.5% at 25 VUs for 60s
k6 run "${STRESS_TESTS_DIR}/k6/approval_events.js"

# Test 3: outbox-executor (worker pressure)
# Pass criteria: no 5xx, no double-claim evidence in DB
k6 run "${STRESS_TESTS_DIR}/k6/outbox_executor.js"
```

### 6b: pgbench DB Stress (requires direct psql access)

```bash
# Setup pgbench tables/data
psql "${SUPABASE_DB_URL}" -f "${STRESS_TESTS_DIR}/pgbench/setup.sql"

# Run outbox claim concurrency test
psql "${SUPABASE_DB_URL}" -f "${STRESS_TESTS_DIR}/pgbench/outbox_claim.sql"
```

---

## Step 7 (OPTIONAL): A2A Inbox Addon

The A2A (Agent-to-Agent) Inbox is a DB-first addon. It adds task queue semantics
on top of the Trust Spine with suite/office identity enforcement.

Reference: `${A2A_ADDON_DIR}/BRIDGE_TO_TRUSTSPINE.md`

### 7a: Apply A2A Migrations (7 files)

These must be applied AFTER all 42 core migrations.

```bash
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020000_a2a_inbox_core.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020100_a2a_inbox_rpcs.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020200_a2a_inbox_rls.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020300_a2a_inbox_hardening.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020400_a2a_inbox_offices.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020500_a2a_receipts_bridge.sql"
psql "${SUPABASE_DB_URL}" -f "${A2A_MIGRATIONS_DIR}/20260116020600_a2a_suites_tenant_bridge.sql"
```

### 7b: Run A2A Tests

```bash
echo "=== A2A: Tenant Isolation ==="
psql "${SUPABASE_DB_URL}" -f "${A2A_ADDON_DIR}/03_TESTS/tenant_isolation.sql"

echo "=== A2A: Claim Concurrency ==="
psql "${SUPABASE_DB_URL}" -f "${A2A_ADDON_DIR}/03_TESTS/claim_concurrency.sql"
```

### 7c: Deploy A2A Edge Functions

The A2A addon does NOT ship pre-built Edge Functions. The `04_EDGE_FUNCTIONS_STUBS/README.md`
provides endpoint guidance only. You must implement these Edge Functions yourself if needed:

| Endpoint | RPC Called |
|----------|-----------|
| `POST /v1/a2a/tasks` | Create task |
| `POST /v1/a2a/tasks/claim` | `app.claim_a2a_tasks` |
| `POST /v1/a2a/tasks/{id}/start` | `app.start_a2a_task` |
| `POST /v1/a2a/tasks/{id}/done` | `app.complete_a2a_task` |
| `POST /v1/a2a/tasks/{id}/fail` | `app.fail_a2a_task` |

Hard rules from the ecosystem docs:
- Do NOT allow direct table writes from clients; use RPC-only for state transitions
- Do NOT trust client-provided `suite_id`/`office_id` without server-side authorization
- Treat `a2a_task_events` as append-only audit trail (bridge into Receipt Ledger if desired)

---

## Mandatory vs Optional Summary

| Component | Status | Count | Step |
|-----------|--------|-------|------|
| Core Migrations | **MANDATORY** | 42 files | Step 1 |
| Bootstrap Smoke Tests | **MANDATORY** | 4 scripts | Step 2 |
| Core Edge Functions | **MANDATORY** | 5 functions + shared utils | Step 3 |
| E2E Tests (numbered) | **MANDATORY** | 5 files | Step 4 |
| Go Receipt Verifier | **MANDATORY** | 1 package (8 source files) | Step 5 |
| k6 Stress Tests | OPTIONAL | 3 scripts | Step 6a |
| pgbench Stress Tests | OPTIONAL | 2 scripts | Step 6b |
| A2A Migrations | OPTIONAL | 7 files | Step 7a |
| A2A Tests | OPTIONAL | 2 files | Step 7b |
| A2A Edge Functions | OPTIONAL (stubs only) | 0 (build yourself) | Step 7c |

---

## Verification Checklist

### Gate: Database Layer

- [x] All 42 core migrations applied without errors (2026-02-10)
- [x] 7 A2A addon migrations applied without errors (2026-02-10)
- [x] RLS verified: 27/27 isolation + evil tests PASS (custom `rls-isolation.sql`)
- [x] 40 tables with FORCE ROW LEVEL SECURITY
- [x] Splinter lint: 0 WARNs (149 INFOs — all expected)

### Gate: Edge Functions

- [x] `approval-events` deployed and listed
- [x] `inbox` deployed and listed
- [x] `outbox-executor` deployed and listed
- [x] `outbox-worker` deployed and listed
- [x] `policy-eval` deployed and listed

### Gate: Desktop Integration (NEW — executed beyond original plan)

- [x] Desktop server refactored: `initDatabase()` removed, RLS middleware added
- [x] Receipts upgraded to Trust Spine 15-column format (hash-chained, UUID suite_id)
- [x] PR #1 merged to swayz032/Aspire-Desktop (SHA: 952cfb4)
- [x] www.aspireos.app live with full backend (health, finance, Stripe connected)
- [x] Security fixes applied (Gusto secret, RLS middleware ordering, header injection, fail-closed)

### Gate: Go Verifier

- [ ] Deferred — hash computation handled by Postgres triggers (`trust_compute_receipt_hash()`)
- [ ] Go verifier available for Phase 1 if needed for external verification

### Gate: Optional Components

- [ ] (Deferred) k6 stress tests — run during Phase 4 hardening
- [ ] (Deferred) pgbench concurrency tests — run during Phase 4 hardening
- [x] A2A migrations applied (7 files) and integrated

---

## Rollback Procedure

If any step fails after partial migration:

```bash
# Option 1: Reset to clean state (DESTRUCTIVE -- development only)
supabase db reset --project-ref "${SUPABASE_PROJECT_REF}"

# Option 2: Point-in-time recovery (production)
# Use Supabase Dashboard > Database > Backups > Restore to a point before migration

# Option 3: Manual rollback (requires writing inverse migrations)
# NOT recommended -- migrations are designed to be forward-only
# Write corrective migrations instead (append-only, per Law #2)
```

---

## Post-Deployment: Aspire Law Compliance Mapping

| Aspire Law | Verified By |
|------------|------------|
| Law #1: Single Brain | Edge Functions are execution-only (no decision logic) |
| Law #2: Receipts for All Actions | `04_receipt_hash_verify.sql`, Go verifier, `receipts_crypto.sql` |
| Law #3: Fail Closed | RLS policies deny by default, capability token expiry |
| Law #4: Risk Tiers | Policy schema (`policy_schema.sql`), `policy-eval` Edge Function |
| Law #5: Capability Tokens | `capability_tokens_schema.sql` + RLS, <60s expiry |
| Law #6: Tenant Isolation | `01_tenant_isolation.sql`, all `*_rls.sql` migrations, RLS bootstrap test |
| Law #7: Tools Are Hands | Edge Functions return results only, no autonomous retry |
| Law #8: Interaction States | `presence_sessions_*.sql`, `05_video_presence_enforcement.sql` |
| Law #9: Security Baselines | `trust_pii_redaction.sql`, `privileged_audit_log_*.sql` |
