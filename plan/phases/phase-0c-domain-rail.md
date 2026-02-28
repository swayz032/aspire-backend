---
phase: "0C"
name: "Domain Rail Foundation (PolarisMail + ResellerClub Infrastructure)"
status: "in_progress"
blocking_phase: "0B"
blocks_phases: ["2"]
duration_estimate: "1 week (parallel with Phase 1 ramp-up)"
gates_targeted: [6, 7]
priority: "high"
hardware_required: "Railway Pro (static outbound IP)"
cost: "$0/mo (Railway Pro already active, ResellerClub dev sandbox)"
---

# PHASE 0C: Domain Rail Foundation

## Objective

Stand up the Domain Rail service on Railway (static outbound IP required for ResellerClub API whitelisting) and create the mail/domains database tables with full RLS + governance compliance. This is infrastructure — not orchestration. Runs parallel with Phase 1 ramp-up.

**Why now:** Railway Pro static IP is ready, ResellerClub API settings page is open, and this foundation must exist before Phase 2 Eli Inbox work begins.

---

## Deployment Architecture

```
[Desktop App — www.aspireos.app]
        |
[Railway — Aspire-Desktop]     [Railway — Domain Rail]
(Express + Expo web)            (TypeScript Express, Static IP)
        |                              |
        |                       +------+------+
        |                       |             |
        |               ResellerClub    EmailArray
        |               (IP-whitelisted)  (Polaris)
        |
[Supabase Postgres]
(Trust Spine DB — receipts, capability_tokens, mail_domains...)
```

**Why split:** Railway provides static outbound IP (required by ResellerClub allowlist). The Desktop app (www.aspireos.app) is the primary customer-facing app already on Railway. Domain Rail is a separate service on Railway for IP-whitelisted provider calls. Neither service calls the other's providers directly — all routing goes through the orchestrator (Law 1: Single Brain) once Phase 1 is complete.

---

## Dependencies

**Requires (Blocking):**
- Phase 0B: Trust Spine Deploy (COMPLETE 2026-02-10 — 49 migrations, 5 Edge Functions, Desktop integrated)
- Railway Pro with static outbound IP

**Blocks (Downstream):**
- Phase 2: Eli Inbox + mail_ops_desk (needs mail tables + Domain Rail operational)

**Parallel With:**
- Phase 1: LangGraph Orchestrator (no dependencies between 0C and 1 until Phase 1 registers mail tools)

---

## Tasks

### 0C-1: Whitelist Railway Static IP in ResellerClub (Day 1)

- [ ] **PHASE0C-TASK-001** Get Railway Static Outbound IP
  - Railway Dashboard → Settings → Networking → Static IPs
  - Record the IP for ResellerClub and EmailArray whitelisting
  - **Verification:** IP visible in Railway dashboard

- [ ] **PHASE0C-TASK-002** Whitelist IP in ResellerClub
  - ResellerClub API Settings → IP Whitelist → Add Railway static IP
  - Test: Make a test API call (domain availability check) from Railway
  - **Verification:** ResellerClub API responds 200 from Railway

---

### 0C-2: Create Domain Rail Service Skeleton (Day 1-2)

- [ ] **PHASE0C-TASK-003** Scaffold Domain Rail TypeScript Express Service
  - Create `domain-rail/` directory in repo
  - TypeScript + Express + strict mode
  - Health endpoint: `GET /health` (liveness + readiness)
  - Structure:
    ```
    domain-rail/
    ├── src/
    │   ├── index.ts              # Express app entry
    │   ├── routes/
    │   │   └── domains.ts        # Domain operations (single route file)
    │   ├── clients/
    │   │   └── resellerclub.ts   # ResellerClub API client
    │   ├── middleware/
    │   │   ├── auth.ts           # S2S auth (ported from handoff)
    │   │   └── policyGate.ts     # Capability token validation
    │   ├── utils/
    │   │   └── retry.ts          # Single retry implementation
    │   └── types/
    │       └── index.ts          # Type definitions
    ├── package.json
    ├── tsconfig.json
    └── Dockerfile
    ```
  - **Verification:** `npm run build` succeeds, `npm start` runs health check

---

### 0C-3: Port ResellerClub Client (Day 2-3)

- [ ] **PHASE0C-TASK-004** Create ResellerClub TypeScript Client
  - Port from handoff package (fix `Math.random()` → `crypto.randomUUID()`)
  - Methods: `checkAvailability()`, `registerDomain()`, `getDnsRecords()`, `addDnsRecord()`
  - All correlation IDs use `crypto.randomUUID()` (not Math.random)
  - Timeout: <10s per API call
  - **Verification:** Domain availability check works from local dev

---

### 0C-4: Port S2S Auth Middleware (Day 3)

- [ ] **PHASE0C-TASK-005** Port S2S Auth from Handoff
  - Source: `Aspire_Mail_Domains_Integration_Handoff/services/aspire-domain-rail/src/middleware/auth.js`
  - Convert to TypeScript
  - Timing-safe comparison, dual-secret rotation support
  - Reject requests without valid S2S token
  - **Verification:** Unauthenticated requests return 401

---

### 0C-5: Create Mail Tables Migration (Day 3-4)

- [ ] **PHASE0C-TASK-006** Migration `20260213000001_mail_tables.sql`
  - **Tables:**
    - `mail_domains` — domain records per suite (UUID suite_id FK to app.suites)
    - `mail_dns_records` — DNS records for domain verification (SPF/DKIM/DMARC/MX)
    - `mail_accounts` — email accounts per office
  - **Governance requirements (ALL mandatory):**
    - `suite_id UUID NOT NULL REFERENCES app.suites(suite_id)` (not TEXT!)
    - `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`
    - `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
    - Dual-path RLS policies using `app.check_suite_access(suite_id)` (matches Phase 0B desktop_tables pattern)
    - SELECT + INSERT + UPDATE + DELETE policies (not SELECT-only!)
    - `service_role` bypass policy
    - Immutability triggers on audit-sensitive columns where appropriate
  - **Verification:** Migration applies cleanly to Supabase, `SELECT * FROM pg_policies` shows all policies

---

### 0C-6: Wire Domain Rail Routes with Policy Gate (Day 4-5)

- [ ] **PHASE0C-TASK-007** Domain Routes with Governance
  - Single route file: `routes/domains.ts` (discard duplicate `resellerclub.js` from handoff)
  - Policy gate middleware validates capability tokens via `trust_consume_capability_token()` RPC
  - NOT just presence checks — actual signature/expiry/scope verification
  - Approval ID validation via `rpc_approve_request()` RPC (not presence-only)
  - All operations emit receipts to Trust Spine `receipts` table via Supabase service_role client
  - NOT `console.log` — actual Trust Spine receipt writes
  - **Endpoints:**
    - `GET /v1/domains` — List domains for suite (GREEN)
    - `GET /v1/domains/check` — Check domain availability (GREEN)
    - `POST /v1/domains` — Purchase domain (RED — requires approval)
    - `POST /v1/domains/:id/dns` — Create DNS record (YELLOW — requires confirmation)
    - `GET /v1/domains/:id/dns` — Verify DNS records (GREEN)
    - `DELETE /v1/domains/:id` — Delete domain (RED — requires approval)
  - **Verification:** Each endpoint returns correct receipt_id

---

### 0C-7: Deploy to Railway + Verify Static Egress (Day 5-6)

- [ ] **PHASE0C-TASK-008** Deploy Domain Rail to Railway
  - Create new Railway service in existing project
  - Set environment variables (RESELLERCLUB_API_KEY, S2S_SECRET, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
  - NO `.env` files in repo — Railway env vars only
  - Verify static outbound IP matches whitelisted IP
  - **Verification:** `GET /health` returns 200, ResellerClub test call succeeds from Railway

---

### 0C-8: RLS Isolation Tests for Mail Tables (Day 6-7)

- [ ] **PHASE0C-TASK-009** Mail RLS Isolation + Evil Tests
  - **Isolation tests:**
    - Suite A cannot read Suite B's mail_domains
    - Suite A cannot read Suite B's mail_accounts
    - Office A cannot read Office B's mail_accounts (same suite)
  - **Evil tests:**
    - Cross-tenant domain claim denied
    - Expired/wrong-scope capability token rejected
    - Fake approval_id rejected
    - SQL injection in domain name field blocked
  - **Structural tests:**
    - All 3 mail tables have RLS ENABLED + FORCED
    - All 3 mail tables have SELECT/INSERT/UPDATE/DELETE policies
    - suite_id is UUID (not TEXT)
  - **Verification:** All tests pass with zero cross-tenant leakage

---

## Blocker Remediations (Applied in This Phase)

| # | Blocker | Fix |
|---|---------|-----|
| 1 | Receipt emission is `console.log` only | Write to Trust Spine `receipts` table via Supabase service_role client |
| 2 | Capability token = presence check only | Validate via `trust_consume_capability_token()` RPC |
| 3 | Approval ID = presence check only | Validate via `rpc_approve_request()` RPC |
| 4 | `suite_id TEXT` vs Aspire's `UUID` | All new migrations use UUID, FK to `app.suites(suite_id)` |
| 5 | Missing FORCE ROW LEVEL SECURITY | All new tables include `ALTER TABLE ... FORCE ROW LEVEL SECURITY` |
| 6 | Migration numbering conflict (001/002) | Renumber to `20260213000001` |
| 7 | Duplicate `resellerclub.js` route | Discard — single route file only (`domains.ts`) |
| 8 | `Math.random()` for correlation IDs | Replace with `crypto.randomUUID()` |
| 9 | Real IP in `.env.example` | No `.env` files in repo — Railway env vars only |
| 11 | No write RLS policies | Add INSERT/UPDATE/DELETE policies (match `desktop_tables` pattern) |
| 12 | Code duplication (withRetry 5x) | Single implementation in `domain-rail/src/utils/retry.ts` |

---

## Risk Tier Assignments (Mail/Domains)

| Operation | Tier | Approval Required? |
|-----------|------|--------------------|
| List/search domains | GREEN | No |
| Check domain availability | GREEN | No |
| Verify DNS records | GREEN | No |
| Create DNS records | YELLOW | Yes (user confirm) |
| Create mail account | YELLOW | Yes (user confirm) |
| Send email | YELLOW | Yes (user confirm) |
| Purchase domain | RED | Yes (explicit authority UI) |
| Transfer domain | RED | Yes (explicit authority UI) |
| Delete domain | RED | Yes (explicit authority UI) |
| Delete mail account | RED | Yes (explicit authority UI) |

---

## Success Criteria

- [ ] `0C-SC-001` Railway static outbound IP whitelisted in ResellerClub
- [ ] `0C-SC-002` Domain Rail service deployed on Railway with health check passing
- [ ] `0C-SC-003` ResellerClub API call succeeds from Domain Rail (domain availability check)
- [ ] `0C-SC-004` S2S auth middleware rejects unauthenticated requests
- [ ] `0C-SC-005` Mail tables created with UUID suite_id + FORCE RLS + dual-path policies
- [ ] `0C-SC-006` All domain endpoints emit receipts to Trust Spine (not console.log)
- [ ] `0C-SC-007` Capability token validation uses `trust_consume_capability_token()` RPC
- [ ] `0C-SC-008` RLS isolation tests pass (zero cross-tenant leakage on mail tables)
- [ ] `0C-SC-009` Evil tests pass (expired tokens, fake approvals, cross-tenant claims rejected)

---

## Critical Files Referenced

| File | Why |
|------|-----|
| `supabase/migrations/20260210000001_trust_spine_bundle.sql` | Receipt schema (L1098-1204), capability tokens (L1490-1617), approval RPCs (L316-351) |
| `supabase/migrations/20260210000002_desktop_tables.sql` | Pattern for new feature tables: dual-path RLS, FORCE RLS, service_role bypass |
| `Aspire-desktop/server/receiptService.ts` | Trust Spine 15-column receipt format (createTrustSpineReceipt) |
| `Aspire-desktop/server/index.ts` | RLS context middleware pattern (SET LOCAL app.current_suite_id) |
| `Aspire_Mail_Domains_Integration_Handoff/services/aspire-domain-rail/src/middleware/auth.js` | Production-quality S2S auth to port |

---

## Estimated Duration

**1 week** (7 working days, parallel with Phase 1 ramp-up)

**Net additional time to roadmap: 0 weeks** — runs in parallel.

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Previous Phase:** [phase-0b-tower-setup.md](phase-0b-tower-setup.md)
- **Parallel Phase:** [phase-1-orchestrator.md](phase-1-orchestrator.md)
- **Next Consumer:** [phase-2-founder-mvp.md](phase-2-founder-mvp.md) (Eli Inbox + mail_ops_desk)

---

**Last Updated:** 2026-02-12
**Status:** IN PROGRESS (migration + service code complete, tests in progress — 2026-02-12)
