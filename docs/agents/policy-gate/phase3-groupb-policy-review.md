# Policy Gate Review — Phase 3 Group B Desktop Production Wiring

**Review Date**: 2026-02-15
**Scope**: Desktop → Orchestrator → Admin Ops production wiring
**Reviewer**: Policy Gate Engineer (Adversarial Mode)
**Previous Fixes**: X-Suite-Id auth on approve/deny, fail-closed error handlers, onboarding bypass blocked

---

## Executive Summary

**Overall Verdict**: **CONDITIONAL PASS** (2 P0 blockers, 5 P1 issues)

Phase 3 Group B Desktop wiring shows **strong enforcement in the orchestrator/admin backend** but **critical gaps in Desktop server authentication**. The Desktop server routes lack proper authentication middleware, creating bypass opportunities for state-changing operations.

### Critical Risk

Desktop server endpoints (`/api/users/:userId`, `/api/authority-queue/:id/approve`, `/api/services/:serviceId`) have **NO authentication checks**, allowing unauthenticated clients to:
- Modify suite profiles (change owner names, business settings)
- Approve/deny authority queue requests without credentials
- Create/update/delete services

This violates **Law #3 (Fail Closed)** and creates a **governance bypass vector**.

### Strengths

- **Admin Ops API**: Strong JWT auth enforcement, fail-closed on every endpoint
- **Orchestrator Intent Proxy**: Enforces X-Suite-Id header (L687-690)
- **Onboarding Gate**: Properly blocks unauthenticated access to main app (L76-78)
- **RLS Context Middleware**: Correctly sets `app.current_suite_id` for tenant isolation (index.ts L46-67)
- **Receipt Generation**: Desktop approve/deny endpoints generate receipts (L806-811, L842-847)

---

## 1. Findings

### Finding 1: Desktop Suite Profile Routes Unauthenticated

- **File**: `Aspire-desktop/server/routes.ts:L21-L69`
- **Severity**: **CRITICAL (P0 BLOCKING)**
- **Invariant**: Fail-Closed Behavior + Tenant Isolation
- **Description**: Suite profile GET/POST/PATCH endpoints have NO auth checks. Any client can read ANY suite's profile, create profiles, or modify existing profiles without credentials.
- **Evidence**:
  ```typescript
  // L21-29: GET /api/suites/:suiteId — NO AUTH CHECK
  router.get('/api/suites/:suiteId', async (req: Request, res: Response) => {
    try {
      const profile = await storage.getSuiteProfile(getParam(req.params.suiteId));
      if (!profile) return res.status(404).json({ error: 'Suite profile not found' });
      res.json(profile);
    } catch (error: any) {
      res.status(500).json({ error: error.message });
    }
  });

  // L61-69: PATCH /api/users/:userId — NO AUTH CHECK
  router.patch('/api/users/:userId', async (req: Request, res: Response) => {
    try {
      const profile = await storage.updateSuiteProfile(getParam(req.params.userId), req.body);
      if (!profile) return res.status(404).json({ error: 'Suite profile not found' });
      res.json(profile);
    } catch (error: any) {
      res.status(500).json({ error: error.message });
    }
  });
  ```

**Attack Vector**: Unauthenticated client can:
1. Enumerate all suite IDs by trying UUIDs
2. Read profile data (business name, owner info, settings) for any suite
3. Modify ANY suite's profile by sending PATCH with arbitrary `userId`
4. Change `owner_email`, `business_name`, `industry` without authorization

**Impact**: **Law #6 (Tenant Isolation)** violation — cross-tenant data access AND modification.

---

### Finding 2: Desktop Authority Queue Routes Unauthenticated (Previous Fix Incomplete)

- **File**: `Aspire-desktop/server/routes.ts:L786-L855`
- **Severity**: **CRITICAL (P0 BLOCKING)**
- **Invariant**: Approval Gates Cannot Be Bypassed
- **Description**: While approve/deny endpoints NOW check for `X-Suite-Id` header (previous fix), they do NOT verify that the header value matches an authenticated session. Any client can forge `X-Suite-Id` header and approve/deny requests for ANY tenant.
- **Evidence**:
  ```typescript
  // L786-790: Approve endpoint — checks header EXISTS, not VALID
  router.post('/api/authority-queue/:id/approve', async (req: Request, res: Response) => {
    const suiteId = req.headers['x-suite-id'] as string;
    if (!suiteId) {
      return res.status(401).json({ error: 'AUTH_REQUIRED', message: 'Missing required header: X-Suite-Id' });
    }
    // NO VERIFICATION that suiteId matches authenticated user!
    // RLS middleware sets app.current_suite_id, but does NOT validate auth
  ```

**Attack Vector**: Unauthenticated client can:
1. Send `POST /api/authority-queue/123/approve` with header `X-Suite-Id: victim-suite-id`
2. Desktop server sets RLS context to victim's suite (index.ts L61)
3. Approval UPDATE succeeds because RLS context matches
4. Receipt is generated with victim's `suite_id`, but NO actor authentication

**Impact**: **Law #4 (Risk Tier Enforcement)** violation — RED-tier approvals can be executed by anyone who knows a suite ID.

---

### Finding 3: Desktop Services Routes Unauthenticated

- **File**: `Aspire-desktop/server/routes.ts:L89-L135`
- **Severity**: **HIGH (P1)**
- **Invariant**: Fail-Closed Behavior
- **Description**: Service creation/update/deletion endpoints have NO auth checks. Also makes direct Stripe API calls without going through orchestrator (violates Law #1).
- **Evidence**:
  ```typescript
  // L89-116: POST /api/users/:userId/services — NO AUTH, CALLS STRIPE DIRECTLY
  router.post('/api/users/:userId/services', async (req: Request, res: Response) => {
    try {
      const stripe = await getUncachableStripeClient();  // Law #1 violation

      const product = await stripe.products.create({
        name: req.body.name,
        description: req.body.description || '',
        metadata: { suiteId: getParam(req.params.userId) },
      });

      const price = await stripe.prices.create({
        product: product.id,
        unit_amount: req.body.price,
        currency: req.body.currency || 'usd',
      });
      // ... creates service record
    } catch (error: any) {
      res.status(500).json({ error: error.message });
    }
  });
  ```

**Attack Vector**: Unauthenticated client can:
1. Create services for any suite by forging `userId` param
2. Trigger Stripe API calls (product creation, price creation) without authorization
3. Delete services for any suite

**Impact**: **Law #1 (Single Brain Rule)** violation — Desktop server executes provider calls directly, not through orchestrator.

---

### Finding 4: Onboarding Bypass via Direct Navigation (Previous Fix Not Fully Deployed)

- **File**: `Aspire-desktop/app/_layout.tsx:L36-L84`
- **Severity**: **HIGH (P1)**
- **Invariant**: Fail-Closed Behavior
- **Description**: While `useAuthGate()` NOW checks `onboarding_completed_at` (L52-65), the check is async and relies on client-side enforcement only. A malicious client can manually navigate to `/(tabs)` route before the onboarding check completes.
- **Evidence**:
  ```typescript
  // L67-83: useEffect triggers AFTER initial render
  useEffect(() => {
    if (isLoading) return;

    const inAuthGroup = segments[0] === '(auth)';
    const onOnboarding = segments[1] === 'onboarding';

    if (!session && !inAuthGroup) {
      router.replace('/(auth)/login');
    } else if (session && onboardingChecked && !onboardingComplete && !onOnboarding) {
      router.replace('/(auth)/onboarding');  // Client-side redirect only
    } else if (session && onboardingChecked && onboardingComplete && inAuthGroup) {
      router.replace('/(tabs)');
    }
  }, [session, isLoading, segments, onboardingChecked, onboardingComplete]);
  ```

**Attack Vector**: User can:
1. Log in successfully
2. Open browser dev tools
3. Before onboarding check completes, run: `window.location.href = '/(tabs)'`
4. Access main app with incomplete `suite_profiles` record

**Impact**: Undefined behavior — main app expects complete suite profile (business name, industry, etc.), but data may be NULL.

---

### Finding 5: Desktop Receipts Missing Actor ID

- **File**: `Aspire-desktop/server/routes.ts:L806-L811, L842-L847`
- **Severity**: **HIGH (P1)**
- **Invariant**: No Action Without a Receipt (Law #2)
- **Description**: Approval/denial receipts are generated but do NOT include `actor_id` field. Admin endpoints correctly bind `actor_id` from JWT (admin.py L229-230), but Desktop server omits this.
- **Evidence**:
  ```typescript
  // L806-811: Approval receipt — NO actor_id field
  await db.execute(sql`
    INSERT INTO receipts (receipt_id, action_type, outcome, reason_code, risk_tier,
                          suite_id, correlation_id, actor_type, executed_at, title)
    VALUES (${receiptId}, 'approval', 'success', 'user_approved', 'yellow',
            ${suiteId}, ${correlationId}, 'user', NOW(),
            (SELECT title FROM approval_requests WHERE id = ${id}))
  `);
  ```

**Impact**: **Audit trail incomplete** — receipts show "user" approved, but not WHICH user. Cannot replay who made decisions.

---

### Finding 6: Error Messages Expose Internal Details

- **File**: `Aspire-desktop/server/routes.ts` (multiple catch blocks)
- **Severity**: **MEDIUM (P2)**
- **Invariant**: Security & Privacy Baselines (Law #9)
- **Description**: Catch blocks return raw `error.message` to client, potentially exposing database schema, internal paths, or provider API errors.
- **Evidence**:
  ```typescript
  // L16-18, L26-28, etc. — 30+ instances
  } catch (error: any) {
    res.status(500).json({ error: error.message });
  }
  ```

**Attack Vector**: Attacker can:
1. Send malformed requests to trigger errors
2. Read error messages to learn database table names, column names, constraints
3. Use info for SQL injection or schema enumeration attacks

**Impact**: **Information disclosure** — aids attackers in reconnaissance phase.

---

### Finding 7: PII in Log Statements

- **File**: `Aspire-desktop/server/routes.ts:L397-L398, L708, L815, L851`
- **Severity**: **MEDIUM (P2)**
- **Invariant**: Security & Privacy Baselines (Law #9)
- **Description**: Console log statements may include PII (business names, voice IDs, error details) without redaction.
- **Evidence**:
  ```typescript
  // L397: Logs business name (could be PII)
  console.log('Audio preview request:', { clipType, reason, businessName, voiceId });

  // L708: Logs orchestrator error (could contain user inputs)
  console.error('Orchestrator error:', response.status, errorText);
  ```

**Impact**: **PII in server logs** — violates Law #9 (never log PII raw).

---

### Finding 8: Timestamp-Based Receipt IDs

- **File**: `Aspire-desktop/server/routes.ts:L803, L839`
- **Severity**: **LOW (P2)**
- **Invariant**: Receipt Immutability (Law #2)
- **Description**: Receipt IDs use `Date.now()` instead of UUIDs, creating collision risk and predictability.
- **Evidence**:
  ```typescript
  // L803
  const receiptId = `RCP-${Date.now()}`;
  ```

**Attack Vector**: Two approvals in same millisecond → same receipt ID → database constraint violation OR silently overwrite.

**Impact**: **Receipt integrity risk** — collisions could cause lost audit trail entries.

---

### Finding 9: No Rate Limiting on Approval Endpoints

- **File**: `Aspire-desktop/server/routes.ts:L786-L855`
- **Severity**: **LOW (P2)**
- **Invariant**: Fail-Closed Behavior
- **Description**: Authority queue approve/deny endpoints lack rate limiting. Attacker with valid (or forged) X-Suite-Id can spam approvals.
- **Evidence**: No rate limit middleware in routes.ts or index.ts

**Impact**: **Denial of service** — attacker can flood approval queue with bogus approvals.

---

### Finding 10: RLS Context Middleware Trusts Client-Provided JWT

- **File**: `Aspire-desktop/server/index.ts:L46-L67`
- **Severity**: **MEDIUM (P1)**
- **Invariant**: Tenant Isolation (Law #6)
- **Description**: RLS context middleware extracts `suite_id` from JWT `user_metadata` but does NOT verify the JWT signature was issued by Supabase. If `SUPABASE_SERVICE_ROLE_KEY` is compromised or missing, any client can forge JWTs.
- **Evidence**:
  ```typescript
  // L52-58: JWT extraction — relies on supabaseAdmin.auth.getUser()
  const authHeader = req.headers.authorization;
  if (authHeader?.startsWith('Bearer ') && supabaseAdmin) {
    const token = authHeader.slice(7);
    const { data: { user }, error } = await supabaseAdmin.auth.getUser(token);
    if (!error && user?.user_metadata?.suite_id) {
      suiteId = user.user_metadata.suite_id;  // Trusts JWT claim
    }
  }
  ```

**Risk**: If `SUPABASE_SERVICE_ROLE_KEY` env var is missing or incorrect, `supabaseAdmin` is `null` (L37-41), and middleware falls back to `defaultSuiteId` (L48). This means:
- **All unauthenticated requests share the same suite context**
- **Cross-tenant isolation fails silently**

**Impact**: **Silent fail-open on auth failure** — violates Law #3 (fail closed).

---

## 2. Bypass Attempts (10 Total)

### Bypass Attempt 1: Onboarding Bypass via Direct Navigation

- **Attack Vector**: After login, manually navigate to `/(tabs)` before onboarding check completes
- **Code Path Traced**:
  1. User logs in → `login.tsx:L43` sets session
  2. `_layout.tsx:L36` `useAuthGate()` hook fires
  3. `L44-65`: Async fetch of `onboarding_completed_at` from Supabase
  4. During fetch, `onboardingChecked = false` → no redirect happens
  5. User navigates to `/(tabs)` → main app loads
  6. Fetch completes → redirect fires, but user already in app
- **Result**: **PARTIAL ⚠️** (Race condition exists, but router.replace() may re-redirect)
- **Evidence**: Client-side routing allows bypass window. Server-side onboarding enforcement missing.

---

### Bypass Attempt 2: Approve Authority Queue Without Auth

- **Attack Vector**: Send `POST /api/authority-queue/123/approve` with forged `X-Suite-Id` header, no JWT
- **Code Path Traced**:
  1. Client sends: `POST /api/authority-queue/abc-123/approve` with header `X-Suite-Id: victim-suite-id`
  2. `index.ts:L46-67`: RLS middleware runs
  3. No `Authorization` header → `suiteId = defaultSuiteId` (L48)
  4. BUT if client provides `X-Suite-Id` header, routes.ts L788 reads it: `req.headers['x-suite-id']`
  5. `L790`: Checks if header exists (it does) → proceeds
  6. `L797-800`: UPDATE succeeds using RLS context from middleware (defaultSuiteId, NOT victim's)
  7. **WAIT**: RLS context is set to `defaultSuiteId`, but UPDATE query has no WHERE clause filtering by suite
- **Re-analysis**:
  - `routes.ts:L797-800` UPDATE has NO `WHERE suite_id = ...` clause
  - RLS policies enforce filtering, but only if approval_requests table has RLS enabled
  - Desktop uses Drizzle ORM + raw SQL, NOT Supabase client (which auto-applies RLS)
  - **Vulnerability depends on whether approval_requests has RLS policy**
- **Result**: **VULNERABLE ❌** (If RLS not enabled on approval_requests table, cross-tenant approval succeeds)
- **Evidence**: No auth verification on routes.ts L788, relies entirely on DB-level RLS.

---

### Bypass Attempt 3: Cross-Tenant Suite Profile Update

- **Attack Vector**: Send `PATCH /api/users/victim-suite-id` with arbitrary body, no auth
- **Code Path Traced**:
  1. Client sends: `PATCH /api/users/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` with body `{"business_name":"Hacked"}`
  2. `index.ts:L46-67`: RLS middleware runs → sets `app.current_suite_id = defaultSuiteId`
  3. `routes.ts:L61-69`: No auth check → calls `storage.updateSuiteProfile(userId, req.body)`
  4. Storage layer uses Drizzle ORM → issues UPDATE query
  5. If `suite_profiles` has RLS: UPDATE fails (wrong suite context)
  6. If `suite_profiles` has NO RLS: UPDATE succeeds (victim's profile modified)
- **Result**: **VULNERABLE ❌** (No auth check, depends on RLS policy existence)
- **Evidence**: routes.ts L61-69 has no auth middleware.

---

### Bypass Attempt 4: Shadow Execution (Desktop Calls Stripe Directly)

- **Attack Vector**: Send `POST /api/users/:userId/services` to create service → triggers Stripe API call
- **Code Path Traced**:
  1. Client sends: `POST /api/users/abc-123/services` with body `{"name":"Fake","price":100}`
  2. `routes.ts:L91`: `const stripe = await getUncachableStripeClient();` → instantiates Stripe client
  3. `L93-97`: `stripe.products.create()` → DIRECT CALL to Stripe API (NOT through orchestrator)
  4. `L99-103`: `stripe.prices.create()` → DIRECT CALL to Stripe API
  5. `L105-110`: Stores service in DB
- **Result**: **VULNERABLE ❌** (Law #1 violation — Desktop executes state-changing provider calls autonomously)
- **Evidence**: routes.ts L91-103 bypasses orchestrator, no capability token, no approval gate.

---

### Bypass Attempt 5: Receipt Tampering (Append-Only Violation)

- **Attack Vector**: Modify or delete receipts table entries
- **Code Path Traced**:
  1. Desktop server inserts receipts via `db.execute(sql\`INSERT INTO receipts...\`)` (routes.ts L806-811)
  2. No UPDATE or DELETE statements in routes.ts
  3. BUT: If DB user has UPDATE/DELETE privileges on receipts table → can tamper
  4. Admin.py uses `store_receipts()` which enforces append-only (no UPDATE)
- **Result**: **BLOCKED ✅** (No code paths in Desktop routes.ts attempt UPDATE/DELETE)
- **Evidence**: Receipts are INSERT-only in application code. DB-level protections required.

---

### Bypass Attempt 6: Risk Tier Downgrade (Approval Receipts Always YELLOW)

- **Attack Vector**: Attempt to approve RED-tier request with YELLOW receipt
- **Code Path Traced**:
  1. Desktop routes.ts L808: Receipts hardcoded as `risk_tier = 'yellow'`
  2. Admin.py L912-928: RED-tier proposals require `presence_token`, enforcement works
  3. Desktop server has NO risk tier checking — always generates YELLOW receipts
  4. BUT: Desktop `/api/authority-queue/:id/approve` does NOT check proposal risk tier before approving
- **Result**: **PARTIAL ⚠️** (Desktop receipts always YELLOW, but no enforcement of RED-tier presence token requirement)
- **Evidence**: Desktop receipts (L808, L844) hardcoded to YELLOW, no risk tier validation.

---

### Bypass Attempt 7: Token Bypass (MCP Tools Called Without Capability Tokens)

- **Attack Vector**: Call orchestrator intent endpoint without capability token
- **Code Path Traced**:
  1. Client sends: `POST /api/orchestrator/intent` with header `X-Suite-Id: abc-123`
  2. `routes.ts:L687-690`: Checks `X-Suite-Id` exists (not valid, just exists)
  3. `L692-704`: Proxies to orchestrator `POST /v1/intents` with `X-Suite-Id` header
  4. Orchestrator receives request → mints capability token internally → calls MCP tool
- **Result**: **BLOCKED ✅** (Orchestrator enforces token minting, Desktop is just a proxy)
- **Evidence**: Desktop does not mint tokens, orchestrator enforces Law #5.

---

### Bypass Attempt 8: Orchestrator Proxy Bypass (Client Calls Orchestrator Directly)

- **Attack Vector**: Client bypasses Desktop server, calls orchestrator directly at `http://localhost:8000/v1/intents`
- **Code Path Traced**:
  1. Desktop server proxies to `ORCHESTRATOR_URL` (routes.ts L686)
  2. If orchestrator is exposed on network, client can call directly
  3. Orchestrator admin.py L687-690 enforces X-Suite-Id header
  4. Admin.py L133-159 `_require_admin()` enforces JWT for admin endpoints
  5. BUT `/v1/intents` endpoint requires X-Suite-Id, NOT admin JWT
- **Result**: **BLOCKED ✅** (Orchestrator enforces X-Suite-Id header, client needs valid suite ID)
- **Evidence**: Orchestrator enforces governance pipeline, Desktop is optional frontend.

---

### Bypass Attempt 9: PII in Logs (Console Logs Expose Business Names)

- **Attack Vector**: Trigger errors to force PII into server logs
- **Code Path Traced**:
  1. Send malformed request to `/api/frontdesk/preview-audio`
  2. `routes.ts:L397`: `console.log('Audio preview request:', { businessName })` → logs business name
  3. Server logs persisted to Railway/Cloud → PII stored unredacted
- **Result**: **VULNERABLE ❌** (Law #9 violation — PII in logs without redaction)
- **Evidence**: routes.ts L397, L708, L815, L851 log potentially sensitive data.

---

### Bypass Attempt 10: Error Info Leakage (500 Errors Return Raw Exception Messages)

- **Attack Vector**: Send malformed SQL query to trigger database error
- **Code Path Traced**:
  1. Send `PATCH /api/users/abc-123` with body `{"business_name":"'; DROP TABLE users--"}`
  2. Storage layer throws exception: `"invalid input syntax for type uuid"`
  3. `routes.ts:L66-68`: `res.status(500).json({ error: error.message })` → returns error to client
  4. Client learns database uses UUIDs, can infer schema
- **Result**: **VULNERABLE ❌** (Law #9 violation — internal error details exposed)
- **Evidence**: 30+ catch blocks return `error.message` without sanitization.

---

## 3. Invariant Scorecard

| Invariant | Status | Evidence |
|-----------|--------|----------|
| **Approval Gates Cannot Be Bypassed** | **FAIL ❌** | Finding 2: Authority queue routes accept forged X-Suite-Id headers without auth verification. Desktop receipts (Finding 5) lack actor_id binding. Finding 6: RED-tier presence token requirement not enforced in Desktop routes. |
| **Capability Tokens Properly Enforced** | **PASS ✅** | Desktop does not mint tokens (Bypass #7 blocked). All provider calls routed through orchestrator (except Finding 3 — Stripe direct calls). Orchestrator enforces token validation. |
| **Fail-Closed Behavior Exists** | **FAIL ❌** | Finding 1: Suite profile routes have NO auth (fail open). Finding 2: Authority queue routes trust unauthenticated headers. Finding 10: RLS middleware silently falls back to defaultSuiteId if auth fails. Finding 4: Onboarding bypass race condition exists. |
| **UI Never Executes Providers Directly** | **FAIL ❌** | Finding 3: Desktop service creation routes call Stripe API directly (L91-103), bypassing orchestrator and violating Law #1. |
| **Risk Tier Classification Enforced** | **PARTIAL ⚠️** | Admin.py enforces RED-tier presence tokens (Bypass #6 shows admin enforcement works). Desktop routes hardcode receipts to YELLOW (L808, L844), no risk tier validation. Risk tier classification exists in orchestrator, not in Desktop server. |

**Overall Verdict**: **CONDITIONAL PASS** (2 P0 blockers must be fixed before production)

---

## 4. Required Fixes

### Fix 1: Add Authentication Middleware to Desktop Server

- **Priority**: **P0 (BLOCKING)** — MUST fix before production
- **Invariant**: Fail-Closed Behavior + Tenant Isolation
- **Location**: `Aspire-desktop/server/routes.ts` (all routes)
- **Current Behavior**: Routes accept requests without authentication. X-Suite-Id header is trusted blindly.
- **Required Behavior**:
  1. Extract JWT from `Authorization: Bearer <token>` header
  2. Validate JWT signature using `supabaseAdmin.auth.getUser(token)`
  3. Extract `suite_id` from JWT `user_metadata`
  4. Verify `X-Suite-Id` header (if present) matches JWT `suite_id`
  5. If auth fails → return 401, generate denial receipt
- **Minimal Change**:
  ```typescript
  // Add to routes.ts BEFORE route definitions
  async function requireAuth(req: Request, res: Response, next: Function) {
    const authHeader = req.headers.authorization;
    const suiteIdHeader = req.headers['x-suite-id'] as string;

    if (!authHeader?.startsWith('Bearer ')) {
      return res.status(401).json({ error: 'AUTH_REQUIRED', message: 'Missing Authorization header' });
    }

    const token = authHeader.slice(7);
    const { data: { user }, error } = await supabaseAdmin.auth.getUser(token);

    if (error || !user) {
      return res.status(401).json({ error: 'INVALID_TOKEN', message: 'Invalid or expired JWT' });
    }

    const jwtSuiteId = user.user_metadata?.suite_id;
    if (!jwtSuiteId) {
      return res.status(401).json({ error: 'MISSING_SUITE_ID', message: 'JWT missing suite_id claim' });
    }

    if (suiteIdHeader && suiteIdHeader !== jwtSuiteId) {
      return res.status(403).json({ error: 'SUITE_MISMATCH', message: 'X-Suite-Id does not match authenticated user' });
    }

    // Store in req for downstream use
    (req as any).authenticatedSuiteId = jwtSuiteId;
    (req as any).authenticatedUserId = user.id;
    next();
  }

  // Apply to state-changing routes
  router.patch('/api/users/:userId', requireAuth, async (req, res) => { /* ... */ });
  router.post('/api/authority-queue/:id/approve', requireAuth, async (req, res) => { /* ... */ });
  router.post('/api/authority-queue/:id/deny', requireAuth, async (req, res) => { /* ... */ });
  router.post('/api/users/:userId/services', requireAuth, async (req, res) => { /* ... */ });
  router.patch('/api/services/:serviceId', requireAuth, async (req, res) => { /* ... */ });
  router.delete('/api/services/:serviceId', requireAuth, async (req, res) => { /* ... */ });
  ```

---

### Fix 2: Bind Actor ID in Desktop Receipts

- **Priority**: **P1 (HIGH)** — Audit trail incomplete without this
- **Invariant**: No Action Without a Receipt (Law #2)
- **Location**: `Aspire-desktop/server/routes.ts:L806-L811, L842-L847`
- **Current Behavior**: Receipts use `actor_type: 'user'` but no `actor_id` field
- **Required Behavior**: Extract `user.id` from authenticated JWT, store in receipt
- **Minimal Change**:
  ```typescript
  // After implementing requireAuth middleware (Fix 1):
  const actorId = (req as any).authenticatedUserId || 'anonymous';

  await db.execute(sql`
    INSERT INTO receipts (receipt_id, action_type, outcome, reason_code, risk_tier,
                          suite_id, correlation_id, actor_type, actor_id, executed_at, title)
    VALUES (${receiptId}, 'approval', 'success', 'user_approved', 'yellow',
            ${suiteId}, ${correlationId}, 'user', ${actorId}, NOW(),
            (SELECT title FROM approval_requests WHERE id = ${id}))
  `);
  ```

---

### Fix 3: Remove Direct Stripe Calls from Desktop Server

- **Priority**: **P0 (BLOCKING)** — Law #1 violation
- **Invariant**: Single Brain Rule + Tools Are Hands
- **Location**: `Aspire-desktop/server/routes.ts:L89-L116`
- **Current Behavior**: Desktop server creates Stripe products/prices directly
- **Required Behavior**: Proxy service creation requests to orchestrator → orchestrator mints capability token → orchestrator calls Stripe via MCP tool
- **Minimal Change**:
  ```typescript
  // Replace L89-116 with orchestrator proxy
  router.post('/api/users/:userId/services', requireAuth, async (req, res) => {
    const suiteId = (req as any).authenticatedSuiteId;
    const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:8000';

    // Proxy to orchestrator
    const response = await fetch(`${ORCHESTRATOR_URL}/v1/services`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Suite-Id': suiteId,
      },
      body: JSON.stringify({
        name: req.body.name,
        description: req.body.description,
        price: req.body.price,
        currency: req.body.currency || 'usd',
      }),
    });

    if (!response.ok) {
      const error = await response.json();
      return res.status(response.status).json(error);
    }

    const service = await response.json();
    res.status(201).json(service);
  });
  ```

**Note**: This requires implementing `/v1/services` endpoint in orchestrator (Phase 3 W7 work).

---

### Fix 4: Server-Side Onboarding Enforcement

- **Priority**: **P1 (HIGH)** — Prevents incomplete suite profiles
- **Invariant**: Fail-Closed Behavior
- **Location**: `Aspire-desktop/server/routes.ts` (all protected routes)
- **Current Behavior**: Onboarding check is client-side only (`_layout.tsx`)
- **Required Behavior**: Auth middleware verifies `onboarding_completed_at` is NOT NULL before allowing access
- **Minimal Change**:
  ```typescript
  // Enhance requireAuth middleware from Fix 1:
  async function requireAuth(req: Request, res: Response, next: Function) {
    // ... existing JWT validation ...

    // Check onboarding status
    const { data: profile } = await supabase
      .from('suite_profiles')
      .select('onboarding_completed_at')
      .eq('suite_id', jwtSuiteId)
      .single();

    if (!profile?.onboarding_completed_at) {
      return res.status(403).json({
        error: 'ONBOARDING_INCOMPLETE',
        message: 'Please complete onboarding before accessing this resource',
        redirect: '/(auth)/onboarding'
      });
    }

    (req as any).authenticatedSuiteId = jwtSuiteId;
    (req as any).authenticatedUserId = user.id;
    next();
  }
  ```

---

### Fix 5: Replace Timestamp Receipt IDs with UUIDs

- **Priority**: **P2 (MEDIUM)** — Prevents collision risk
- **Invariant**: Receipt Immutability (Law #2)
- **Location**: `Aspire-desktop/server/routes.ts:L803, L839`
- **Current Behavior**: `const receiptId = \`RCP-${Date.now()}\`;`
- **Required Behavior**: Use crypto-secure UUID
- **Minimal Change**:
  ```typescript
  import { randomUUID } from 'crypto';

  // L803, L839
  const receiptId = randomUUID();  // No prefix needed, UUIDs are unique
  ```

---

### Fix 6: Sanitize Error Messages

- **Priority**: **P2 (MEDIUM)** — Prevents info disclosure
- **Invariant**: Security & Privacy Baselines (Law #9)
- **Location**: All catch blocks in `routes.ts`
- **Current Behavior**: `res.status(500).json({ error: error.message });`
- **Required Behavior**: Return generic error message to client, log full error server-side
- **Minimal Change**:
  ```typescript
  // Replace all catch blocks:
  } catch (error: any) {
    console.error('Route error:', req.path, error);  // Log full error
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'An internal error occurred. Please try again or contact support.',
      correlation_id: req.headers['x-correlation-id'] || randomUUID()
    });
  }
  ```

---

### Fix 7: Redact PII in Log Statements

- **Priority**: **P2 (MEDIUM)** — Law #9 compliance
- **Invariant**: Security & Privacy Baselines
- **Location**: `routes.ts:L397, L708, L815, L851`
- **Current Behavior**: Logs business names, error details without redaction
- **Required Behavior**: Replace PII with placeholders before logging
- **Minimal Change**:
  ```typescript
  // L397: Redact businessName
  console.log('Audio preview request:', {
    clipType,
    reason,
    businessName: businessName ? '<REDACTED>' : null,
    voiceId
  });

  // L815, L851: Remove error.message from logs
  console.warn('approve failed:', { requestId: id, error: 'See correlation_id for details' });
  ```

---

### Fix 8: Add Rate Limiting to Approval Endpoints

- **Priority**: **P2 (MEDIUM)** — Prevents abuse
- **Invariant**: Fail-Closed Behavior
- **Location**: `Aspire-desktop/server/routes.ts:L786, L821`
- **Current Behavior**: No rate limits
- **Required Behavior**: Max 10 approvals per minute per suite
- **Minimal Change**:
  ```typescript
  import rateLimit from 'express-rate-limit';

  const approvalLimiter = rateLimit({
    windowMs: 60 * 1000,  // 1 minute
    max: 10,  // 10 requests per window
    keyGenerator: (req) => (req as any).authenticatedSuiteId || req.ip,
    handler: (req, res) => {
      res.status(429).json({
        error: 'RATE_LIMIT_EXCEEDED',
        message: 'Too many approval requests. Please wait before trying again.'
      });
    },
  });

  router.post('/api/authority-queue/:id/approve', requireAuth, approvalLimiter, async (req, res) => { /* ... */ });
  router.post('/api/authority-queue/:id/deny', requireAuth, approvalLimiter, async (req, res) => { /* ... */ });
  ```

---

### Fix 9: Fail-Closed on Missing SUPABASE_SERVICE_ROLE_KEY

- **Priority**: **P1 (HIGH)** — Silent fail-open is dangerous
- **Invariant**: Fail-Closed Behavior (Law #3)
- **Location**: `Aspire-desktop/server/index.ts:L36-L67`
- **Current Behavior**: If `supabaseAdmin` is `null`, middleware falls back to `defaultSuiteId` (silent fail-open)
- **Required Behavior**: If auth client unavailable, reject ALL requests with 503
- **Minimal Change**:
  ```typescript
  // L46-67: RLS context middleware
  app.use(async (req, res, next) => {
    try {
      // Fail closed if auth client missing
      if (!supabaseAdmin) {
        console.error('CRITICAL: SUPABASE_SERVICE_ROLE_KEY not configured — denying all requests');
        return res.status(503).json({
          error: 'AUTH_UNAVAILABLE',
          message: 'Authentication service unavailable. Please contact support.'
        });
      }

      let suiteId = defaultSuiteId;

      const authHeader = req.headers.authorization;
      if (authHeader?.startsWith('Bearer ')) {
        const token = authHeader.slice(7);
        const { data: { user }, error } = await supabaseAdmin.auth.getUser(token);
        if (!error && user?.user_metadata?.suite_id) {
          suiteId = user.user_metadata.suite_id;
        } else {
          // Fail closed on invalid token
          return res.status(401).json({
            error: 'INVALID_TOKEN',
            message: 'Invalid or expired authentication token'
          });
        }
      }

      if (suiteId) {
        await db.execute(sql`SELECT set_config('app.current_suite_id', ${suiteId}, true)`);
      }
      next();
    } catch (error) {
      console.error('RLS middleware error:', error);
      res.status(500).json({ error: 'INTERNAL_ERROR', message: 'Request authentication failed' });
    }
  });
  ```

---

### Fix 10: Add Risk Tier Validation to Desktop Approval Routes

- **Priority**: **P1 (HIGH)** — RED-tier approvals must enforce presence token
- **Invariant**: Risk Tier Classification Enforced (Law #4)
- **Location**: `Aspire-desktop/server/routes.ts:L786-L819`
- **Current Behavior**: Desktop approve route does NOT check proposal risk tier or require presence token for RED
- **Required Behavior**: Fetch approval request, check risk tier, deny if RED + no presence token
- **Minimal Change**:
  ```typescript
  router.post('/api/authority-queue/:id/approve', requireAuth, async (req, res) => {
    const suiteId = (req as any).authenticatedSuiteId;
    const actorId = (req as any).authenticatedUserId;
    const { id } = req.params;
    const { presence_token } = req.body;

    try {
      // Fetch approval request to check risk tier
      const approvalResult = await db.execute(sql`
        SELECT risk_tier FROM approval_requests WHERE id = ${id}
      `);
      const approval = (approvalResult.rows || approvalResult)[0];

      if (!approval) {
        return res.status(404).json({ error: 'NOT_FOUND', message: 'Approval request not found' });
      }

      // Law #4: RED tier requires presence token
      if (approval.risk_tier === 'red' && !presence_token) {
        const denialReceipt = randomUUID();
        await db.execute(sql`
          INSERT INTO receipts (receipt_id, action_type, outcome, reason_code, risk_tier,
                                suite_id, correlation_id, actor_type, actor_id, executed_at, title)
          VALUES (${denialReceipt}, 'approval_denied', 'denied', 'missing_presence_token', 'red',
                  ${suiteId}, ${req.headers['x-correlation-id'] || randomUUID()}, 'user', ${actorId}, NOW(),
                  (SELECT title FROM approval_requests WHERE id = ${id}))
        `);
        return res.status(403).json({
          error: 'PRESENCE_REQUIRED',
          message: 'RED-tier approvals require presence_token (video verification)',
          receipt_id: denialReceipt
        });
      }

      // Proceed with approval (existing code L797-813)
      // ...
    } catch (error: any) {
      console.error('Approve failed:', { requestId: id, error });
      res.status(500).json({ error: 'APPROVE_FAILED', message: 'Failed to approve request' });
    }
  });
  ```

---

## 5. Production Readiness Assessment

### Blocking Issues (MUST fix before production)

1. **Fix 1** (P0): Add authentication middleware to Desktop server
2. **Fix 3** (P0): Remove direct Stripe calls from Desktop server

### High Priority (SHOULD fix before production)

3. **Fix 2** (P1): Bind actor_id in Desktop receipts
4. **Fix 4** (P1): Server-side onboarding enforcement
5. **Fix 9** (P1): Fail-closed on missing auth client
6. **Fix 10** (P1): Risk tier validation for RED approvals

### Medium Priority (Fix in Phase 3 hardening sprint)

7. **Fix 5** (P2): Replace timestamp receipt IDs with UUIDs
8. **Fix 6** (P2): Sanitize error messages
9. **Fix 7** (P2): Redact PII in logs
10. **Fix 8** (P2): Rate limiting on approval endpoints

---

## 6. Comparison: Desktop Server vs. Admin Ops API

**Admin Ops API Strengths (backend/orchestrator/routes/admin.py)**:
- ✅ **JWT auth enforcement**: `_require_admin()` helper (L133-159) validates EVERY endpoint
- ✅ **Fail-closed**: Missing/invalid JWT → 401, no silent fallback
- ✅ **Actor binding**: Receipts include `actor_id` from JWT (L229-230)
- ✅ **Tenant isolation**: `suite_id` query param REQUIRED for receipt queries (L468-489)
- ✅ **RED-tier presence enforcement**: Proposal approval checks `presence_token` (L911-928)
- ✅ **Receipt generation**: ALL endpoints generate access receipts (Law #2)

**Desktop Server Gaps (Aspire-desktop/server/routes.ts)**:
- ❌ **No auth middleware**: Routes trust client-provided headers
- ❌ **No actor binding**: Receipts missing `actor_id` field
- ❌ **No fail-closed**: RLS middleware falls back to `defaultSuiteId` if auth fails
- ❌ **No risk tier validation**: Approval routes don't check RED vs YELLOW
- ❌ **Shadow execution**: Direct Stripe API calls (Law #1 violation)
- ⚠️ **Partial receipt coverage**: Approve/deny generate receipts, but profile updates don't

**Recommendation**: **Adopt Admin Ops auth patterns for Desktop server**. Specifically:
1. Port `_require_admin()` logic to TypeScript as `requireAuth()` middleware
2. Generate receipts for ALL state-changing operations (not just approve/deny)
3. Add risk tier enforcement logic from admin.py L911-928 to Desktop approve routes
4. Remove all direct provider calls — proxy to orchestrator instead

---

## 7. Memory Update

Updating agent memory with Phase 3 Group B findings:

**Key Patterns Discovered**:
- **Desktop Server Auth Gap**: No authentication middleware on state-changing routes → P0 blocker
- **Admin Ops Strong Enforcement**: JWT auth, fail-closed, actor binding, RED-tier presence tokens
- **RLS Middleware Weakness**: Falls back to `defaultSuiteId` if auth unavailable (silent fail-open)
- **Shadow Execution**: Desktop service creation calls Stripe directly (Law #1 violation)
- **Receipt Inconsistency**: Approve/deny generate receipts, but profile updates don't

**Enforcement Locations**:
- **Admin API auth**: `backend/orchestrator/routes/admin.py:L133-159` (`_require_admin()`)
- **Desktop RLS context**: `Aspire-desktop/server/index.ts:L46-67` (middleware sets `app.current_suite_id`)
- **Desktop approve/deny**: `Aspire-desktop/server/routes.ts:L786-L855` (X-Suite-Id check, receipt generation)
- **Onboarding gate**: `Aspire-desktop/app/_layout.tsx:L36-L84` (client-side only)

**Bypass Vectors**:
- **Forged X-Suite-Id**: Desktop routes trust header without JWT validation
- **Onboarding race condition**: Client can navigate to `/(tabs)` before async check completes
- **Direct Stripe calls**: Desktop bypasses orchestrator for service creation (L89-116)
- **Missing actor_id**: Receipts don't bind WHO approved (only THAT approval happened)

**Production Blockers**:
1. Add authentication middleware to Desktop server (Fix 1)
2. Remove direct Stripe calls from Desktop server (Fix 3)

**Carry-Forward to Phase 3 Hardening**:
- Server-side onboarding enforcement (Fix 4)
- Fail-closed on missing auth client (Fix 9)
- Risk tier validation for RED approvals (Fix 10)
- Rate limiting on approval endpoints (Fix 8)

---

**END OF REVIEW**
