/**
 * Evil/Negative Tests: ElevenLabs V1 Hybrid Architecture Migration
 *
 * Validates governance constraints for the Pass 1-3 ElevenLabs migration:
 * - elevenlabs-auth.ts  (Law #3: fail-closed, timing-safe comparison)
 * - elevenlabs-tools.ts (Law #6: tenant isolation, capability token validation)
 * - elevenlabs-webhooks.ts (Law #2: receipt, Law #3: HMAC validation)
 * - server.ts route mounting (Law #3: no auth bypass via dual mount)
 *
 * Every test in this file is adversarial — it attacks a governance constraint.
 * All should PASS (i.e., the system correctly blocks the attack).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'crypto';
import request from 'supertest';

// ---------------------------------------------------------------------------
// Mock orchestrator client (isolate gateway behavior)
// ---------------------------------------------------------------------------

vi.mock('../services/orchestrator-client.js', () => {
  class MockOrchestratorClientError extends Error {
    code: string;
    correlationId: string;
    constructor(message: string, code: string, correlationId: string) {
      super(message);
      this.name = 'OrchestratorClientError';
      this.code = code;
      this.correlationId = correlationId;
    }
  }
  return {
    proxyToOrchestrator: vi.fn(),
    checkOrchestratorHealth: vi.fn().mockResolvedValue(true),
    checkOrchestratorReadiness: vi.fn().mockResolvedValue({
      reachable: true, httpStatus: 200, status: 'ready', dependency: 'healthy',
    }),
    OrchestratorClientError: MockOrchestratorClientError,
  };
});

vi.mock('../services/incident-reporter.js', () => ({
  reportGatewayIncident: vi.fn().mockResolvedValue(undefined),
}));

// Mock fetch for ElevenLabs API calls
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

const { app } = await import('../server.js');
const { proxyToOrchestrator } = await import('../services/orchestrator-client.js');
const mockProxy = vi.mocked(proxyToOrchestrator);

// ---------------------------------------------------------------------------
// Test constants
// ---------------------------------------------------------------------------

const TOOL_SECRET = 'test-tool-secret-32chars-minimum!!';
const WEBHOOK_SECRET = 'test-webhook-secret-32chars-min!!';

/** Dev-mode JWT headers (used for /v1/sessions path) */
const DEV_JWT_HEADERS = {
  'x-suite-id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  'x-office-id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  'x-actor-id': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
};

/** Valid UUID suite IDs for tool auth tests (auth middleware enforces UUID format) */
const SUITE_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const SUITE_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
const USER_A = 'cccccccc-cccc-cccc-cccc-cccccccccccc';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeHmac(secret: string, body: string): string {
  return crypto.createHmac('sha256', secret).update(body).digest('hex');
}

function buildToolBody(suiteId = SUITE_A, userId = USER_A) {
  return { suite_id: suiteId, user_id: userId };
}

// ---------------------------------------------------------------------------
// CATEGORY 1: Auth Middleware (elevenlabs-auth.ts)
// ---------------------------------------------------------------------------

describe('Law #3: ElevenLabs Tool Auth — Fail-Closed Scenarios', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    // Mock successful orchestrator proxy for context endpoint
    mockProxy.mockResolvedValue({ status: 200, body: { result: 'ok' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
  });

  it('test_tool_auth_missing_secret_header_returns_401', async () => {
    /**
     * Evil test: Tool call with no x-elevenlabs-secret header.
     * Law #3: Must be denied — fail-closed.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('AUTH_FAILED');
  });

  it('test_tool_auth_wrong_secret_returns_401', async () => {
    /**
     * Evil test: Tool call with incorrect shared secret.
     * Law #3: Must be denied even if format is valid.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', 'totally-wrong-secret-value-here!!')
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('AUTH_FAILED');
  });

  it('test_tool_auth_empty_secret_header_returns_401', async () => {
    /**
     * Evil test: Tool call with empty x-elevenlabs-secret header.
     * Law #3: Empty header must not pass as valid.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', '')
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('AUTH_FAILED');
  });

  it('test_tool_auth_unconfigured_secret_returns_500_not_401', async () => {
    /**
     * Evil test: ELEVENLABS_TOOL_SECRET env var is absent.
     * Law #3: Fail-closed means 500 INTERNAL_ERROR (not silently passing).
     * This ensures a misconfigured deployment cannot accept tool calls.
     */
    delete process.env.ELEVENLABS_TOOL_SECRET;

    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(500);
    expect(resp.body.error).toBe('INTERNAL_ERROR');
    // Must NOT pass through to orchestrator
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_tool_auth_timing_safe_prefix_match_returns_401', async () => {
    /**
     * Evil test: Secret that is a prefix of the real secret.
     * A naive string.startsWith() check would pass this.
     * Timing-safe comparison with length check must block it.
     */
    const prefixAttack = TOOL_SECRET.slice(0, 10); // "test-tool-"

    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', prefixAttack)
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(401);
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 2: Tenant Isolation (Law #6)
// ---------------------------------------------------------------------------

describe('Law #6: Tenant Isolation — Tool Endpoints', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    mockProxy.mockResolvedValue({ status: 200, body: { result: 'ok' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
  });

  it('test_tool_missing_suite_id_returns_400', async () => {
    /**
     * Evil test: Tool call without suite_id in body.
     * Law #6: suite_id is required for tenant isolation — must fail.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ user_id: USER_A, query: 'no suite here' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(resp.body.message).toContain('suite_id');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_tool_empty_suite_id_returns_400', async () => {
    /**
     * Evil test: suite_id present but empty string.
     * Law #6: Empty suite_id is equivalent to missing — must fail.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: '', user_id: USER_A, query: 'test' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('test_tool_suite_id_from_auth_not_body_for_context', async () => {
    /**
     * Positive isolation test: verify the orchestrator proxy receives the
     * suite_id from the auth context (set by middleware from body), NOT from
     * a separate body field that an attacker could manipulate after auth.
     * Law #6: tenant context must be middleware-enforced.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'my invoices' });

    expect(resp.status).toBe(200);

    // Verify proxy was called with suite_id in body matching auth context
    expect(mockProxy).toHaveBeenCalledWith(
      expect.objectContaining({
        suiteId: SUITE_A,
        body: expect.objectContaining({
          suite_id: SUITE_A,
        }),
      }),
    );
  });

  it('test_tool_search_invalid_domain_returns_400', async () => {
    /**
     * Evil test: Search with unsupported domain to probe for injection.
     * Must be rejected with validation error, not proxied.
     */
    const resp = await request(app)
      .post('/v1/tools/search')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        query: 'find something',
        domain: '../../../admin', // Path traversal attempt
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_tool_search_sql_injection_domain_rejected', async () => {
    /**
     * Evil test: SQL injection string as domain value.
     * Domain whitelist must block it before proxying.
     */
    const resp = await request(app)
      .post('/v1/tools/search')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        query: "'; DROP TABLE receipts; --",
        domain: "'; DROP TABLE receipts; --",
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 3: Capability Token Enforcement (Law #5)
// ---------------------------------------------------------------------------

describe('Law #5: Capability Token — Execute Endpoint', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    mockProxy.mockResolvedValue({ status: 200, body: { result: 'ok' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
  });

  it('test_execute_without_capability_token_returns_400', async () => {
    /**
     * Evil test: Execute action with no capability_token.
     * Law #5: capability_token is mandatory for RED-tier execution.
     */
    const resp = await request(app)
      .post('/v1/tools/execute')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        action: 'send_invoice',
        params: { invoice_id: 'inv-123' },
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(resp.body.message).toContain('capability_token');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_execute_without_params_returns_400', async () => {
    /**
     * Evil test: Execute with capability_token but no params object.
     * Must be rejected before proxying.
     */
    const resp = await request(app)
      .post('/v1/tools/execute')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        capability_token: 'tok-abc123',
        action: 'send_invoice',
        // params missing
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_execute_with_null_params_rejected', async () => {
    /**
     * Evil test: params: null should not pass the object check.
     */
    const resp = await request(app)
      .post('/v1/tools/execute')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        capability_token: 'tok-abc123',
        action: 'send_invoice',
        params: null,
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 4: Approval Bypass (Law #3 + Law #4)
// ---------------------------------------------------------------------------

describe('Law #3 + #4: Approval Bypass — Draft and Approve Endpoints', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    mockProxy.mockResolvedValue({ status: 200, body: { result: 'ok', draft_id: 'draft-001' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
  });

  it('test_draft_missing_action_returns_400', async () => {
    /**
     * Evil test: Draft without specifying action type.
     * Gateway must reject before proxying to orchestrator.
     */
    const resp = await request(app)
      .post('/v1/tools/draft')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        params: { recipient: 'test@example.com' },
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_draft_always_sets_requires_confirmation_true', async () => {
    /**
     * Law #4: YELLOW-tier draft must always return requires_confirmation: true.
     * This ensures the agent cannot skip user confirmation.
     */
    const resp = await request(app)
      .post('/v1/tools/draft')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        action: 'send_email',
        params: { to: 'test@example.com', subject: 'Test' },
      });

    expect(resp.status).toBe(200);
    expect(resp.body.requires_confirmation).toBe(true);
  });

  it('test_approve_missing_draft_id_returns_400', async () => {
    /**
     * Evil test: Approve action without draft_id.
     * Cannot approve something that was never drafted — must fail closed.
     */
    const resp = await request(app)
      .post('/v1/tools/approve')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        action: 'send_email',
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_approve_empty_draft_id_returns_400', async () => {
    /**
     * Evil test: Approve with empty string draft_id.
     * Empty draft_id must not bypass the check.
     */
    const resp = await request(app)
      .post('/v1/tools/approve')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({
        suite_id: SUITE_A,
        user_id: USER_A,
        draft_id: '',
        action: 'send_email',
      });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 5: Webhook Signature Verification (Law #2 + Law #3)
// ---------------------------------------------------------------------------

describe('Law #2 + #3: Webhook HMAC Signature Validation', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_WEBHOOK_SECRET = WEBHOOK_SECRET;
    mockProxy.mockResolvedValue({ status: 200, body: { status: 'stored' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_WEBHOOK_SECRET;
  });

  it('test_webhook_missing_signature_header_returns_401', async () => {
    /**
     * Evil test: Webhook without elevenlabs-signature header.
     * Law #3: fail-closed — unsigned webhooks must be rejected.
     */
    const body = JSON.stringify({ conversation_id: 'conv-123', transcript: [] });

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .send(body);

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('WEBHOOK_SIGNATURE_INVALID');
  });

  it('test_webhook_wrong_signature_returns_401', async () => {
    /**
     * Evil test: Webhook with a forged/incorrect HMAC signature.
     * Law #3: Signature mismatch must be denied.
     */
    const bodyObj = { conversation_id: 'conv-123', transcript: [] };
    const bodyStr = JSON.stringify(bodyObj);
    const wrongSig = crypto.createHmac('sha256', 'wrong-secret').update(bodyStr).digest('hex');

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .set('elevenlabs-signature', wrongSig)
      .send(bodyStr);

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('WEBHOOK_SIGNATURE_INVALID');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_webhook_tampered_body_with_valid_original_sig_returns_401', async () => {
    /**
     * Evil test: Valid signature for original body, but body was modified.
     * HMAC must catch the tampering.
     */
    const originalBody = JSON.stringify({ conversation_id: 'conv-123', transcript: [] });
    const tamperedBody = JSON.stringify({ conversation_id: 'conv-999', transcript: ['malicious data'] });

    // Sig computed over original body, but tampered body sent
    const validSigForOriginal = makeHmac(WEBHOOK_SECRET, originalBody);

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .set('elevenlabs-signature', validSigForOriginal)
      .send(tamperedBody);

    expect(resp.status).toBe(401);
    expect(resp.body.error).toBe('WEBHOOK_SIGNATURE_INVALID');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_webhook_unconfigured_secret_returns_500_not_process', async () => {
    /**
     * Evil test: ELEVENLABS_WEBHOOK_SECRET not set in environment.
     * Law #3: fail-closed — must return 500, never process.
     */
    delete process.env.ELEVENLABS_WEBHOOK_SECRET;

    const bodyStr = JSON.stringify({ conversation_id: 'conv-123', transcript: [] });
    const sig = makeHmac('any-secret', bodyStr);

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .set('elevenlabs-signature', sig)
      .send(bodyStr);

    expect(resp.status).toBe(500);
    expect(resp.body.error).toBe('INTERNAL_ERROR');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_webhook_missing_conversation_id_returns_400', async () => {
    /**
     * Evil test: Valid signature but payload missing conversation_id.
     * Schema validation must catch this after signature passes.
     */
    const bodyObj = { transcript: [{ role: 'user', message: 'hello' }] };
    const bodyStr = JSON.stringify(bodyObj);
    const sig = makeHmac(WEBHOOK_SECRET, bodyStr);

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .set('elevenlabs-signature', sig)
      .send(bodyStr);

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_webhook_valid_signature_proxies_to_orchestrator', async () => {
    /**
     * Positive test: Valid webhook must be proxied to orchestrator (Law #2 receipt).
     * The gateway should return 200 and hand off to orchestrator for receipt generation.
     */
    const bodyObj = {
      conversation_id: 'conv-valid-123',
      transcript: [{ role: 'user', message: 'schedule a meeting' }],
      metadata: { suite_id: SUITE_A, user_id: USER_A },
    };
    const bodyStr = JSON.stringify(bodyObj);
    const sig = makeHmac(WEBHOOK_SECRET, bodyStr);

    const resp = await request(app)
      .post('/v1/webhooks/elevenlabs/transcripts')
      .set('Content-Type', 'application/json')
      .set('elevenlabs-signature', sig)
      .send(bodyStr);

    expect(resp.status).toBe(200);
    expect(resp.body.status).toBe('received');
    expect(resp.body.conversation_id).toBe('conv-valid-123');
    expect(mockProxy).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/v1/webhooks/elevenlabs/transcripts',
        method: 'POST',
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 6: Agent Name Validation (Law #3: fail-closed)
// ---------------------------------------------------------------------------

describe('Law #3: Signed URL — Agent Name Validation', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_AGENT_AVA = 'el-agent-ava-test-id';
    process.env.ELEVENLABS_API_KEY = 'test-api-key';
    // Signed URL comes from /v1/sessions path which requires JWT auth (dev mode)
    mockProxy.mockResolvedValue({ status: 200, body: { first_name: 'Test', last_name: 'User' } });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ signed_url: 'wss://signedurl.elevenlabs.io/session?token=abc' }),
      text: async () => '',
    });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_AGENT_AVA;
    delete process.env.ELEVENLABS_API_KEY;
  });

  it('test_signed_url_unknown_agent_name_returns_400', async () => {
    /**
     * Evil test: Request a signed URL for an unknown agent.
     * Only valid agent names should be accepted.
     * elevenlabs-sessions.ts returns INVALID_AGENT (security-hardened router).
     */
    const resp = await request(app)
      .post('/v1/sessions/signed-url')
      .set(DEV_JWT_HEADERS)
      .send({ agent: 'mallory_agent' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('INVALID_AGENT');
    expect(resp.body.message).toContain('mallory_agent');
  });

  it('test_signed_url_empty_agent_name_returns_400', async () => {
    /**
     * Evil test: Empty string agent — must not pass validation.
     * After toLowerCase().trim(), empty string is not in VALID_AGENTS set.
     */
    const resp = await request(app)
      .post('/v1/sessions/signed-url')
      .set(DEV_JWT_HEADERS)
      .send({ agent: '' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('INVALID_AGENT');
  });

  it('test_signed_url_prototype_pollution_attempt_rejected', async () => {
    /**
     * Evil test: Prototype pollution via __proto__ agent name.
     * Must be rejected — not in the valid agents Set.
     * The Set lookup (not a plain object) prevents prototype pollution.
     */
    const resp = await request(app)
      .post('/v1/sessions/signed-url')
      .set(DEV_JWT_HEADERS)
      .send({ agent: '__proto__' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('INVALID_AGENT');
  });

  it('test_signed_url_unconfigured_agent_env_returns_503_not_200', async () => {
    /**
     * Evil test: Valid agent name but its env var is not configured.
     * Law #3: fail-closed — missing config must NOT fall back to a default.
     * elevenlabs-sessions.ts returns 503 AGENT_NOT_CONFIGURED for this case.
     */
    delete process.env.ELEVENLABS_AGENT_AVA; // Remove the config

    const resp = await request(app)
      .post('/v1/sessions/signed-url')
      .set(DEV_JWT_HEADERS)
      .send({ agent: 'ava' });

    expect(resp.status).toBe(503);
    expect(resp.body.error).toBe('AGENT_NOT_CONFIGURED');
    expect(resp.body.message).toContain('ELEVENLABS_AGENT_AVA');
    // Must not have called ElevenLabs API
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 7: Route Mounting Security (Dual-mount vulnerability)
// ---------------------------------------------------------------------------

describe('SECURITY: Dual Route Mount — Tool Secret Cannot Access Signed-URL', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    process.env.ELEVENLABS_AGENT_AVA = 'el-agent-ava-test-id';
    process.env.ELEVENLABS_API_KEY = 'test-api-key';
    mockProxy.mockResolvedValue({ status: 200, body: { first_name: 'Test' } });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ signed_url: 'wss://test.elevenlabs.io/abc' }),
      text: async () => '',
    });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
    delete process.env.ELEVENLABS_AGENT_AVA;
    delete process.env.ELEVENLABS_API_KEY;
  });

  it('test_v1_tools_signed_url_with_tool_secret_is_blocked_by_missing_suite_id', async () => {
    /**
     * Security test: ElevenLabs tool calls reach /v1/tools/* with only the shared secret.
     * The tool middleware extracts suite_id from the body — so a signed-url request
     * via /v1/tools/signed-url must include suite_id in the body (not from JWT).
     * This is the "dual mount" path and it must require suite_id.
     */
    const resp = await request(app)
      .post('/v1/tools/signed-url')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ agent: 'ava' }); // No suite_id

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(resp.body.message).toContain('suite_id');
  });
});

// ---------------------------------------------------------------------------
// CATEGORY 8: Context Query Validation
// ---------------------------------------------------------------------------

describe('Law #3: Context Tool — Input Validation', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.ELEVENLABS_TOOL_SECRET = TOOL_SECRET;
    mockProxy.mockResolvedValue({ status: 200, body: { context: 'ok' } });
  });

  afterEach(() => {
    delete process.env.ELEVENLABS_TOOL_SECRET;
  });

  it('test_context_missing_query_returns_400', async () => {
    /**
     * Evil test: Context call without query field.
     * Must be rejected before proxying.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: SUITE_A, user_id: USER_A });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_context_empty_query_returns_400', async () => {
    /**
     * Evil test: Empty query string.
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: SUITE_A, user_id: USER_A, query: '' });

    expect(resp.status).toBe(400);
    expect(resp.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('test_context_valid_request_proxies_correctly', async () => {
    /**
     * Positive test: Valid context request flows through to orchestrator
     * with correct suite_id in body (tenant isolation).
     */
    const resp = await request(app)
      .post('/v1/tools/context')
      .set('x-elevenlabs-secret', TOOL_SECRET)
      .send({ suite_id: SUITE_A, user_id: USER_A, query: 'what is on my calendar today?' });

    expect(resp.status).toBe(200);
    expect(mockProxy).toHaveBeenCalledWith(
      expect.objectContaining({
        suiteId: SUITE_A,
        path: '/v1/intents',
        method: 'POST',
        body: expect.objectContaining({
          intent: 'context_lookup',
          suite_id: SUITE_A,
          query: 'what is on my calendar today?',
        }),
      }),
    );
  });
});
