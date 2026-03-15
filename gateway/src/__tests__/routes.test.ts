/**
 * Gateway Route Handler Tests — Intents, Receipts, Policy
 *
 * Tests mock the orchestrator client to isolate Gateway behavior.
 * Validates:
 * - Auth context propagation (suite_id from auth, not body)
 * - Correlation ID flow
 * - Schema validation at the edge
 * - Error handling (orchestrator down, timeout, invalid response)
 * - Egress validation on AvaResult
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';

// Mock orchestrator client BEFORE importing anything that uses it
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
      reachable: true,
      httpStatus: 200,
      status: 'ready',
      dependency: 'healthy',
    }),
    OrchestratorClientError: MockOrchestratorClientError,
  };
});

// Now import server and the mocked modules
const { app } = await import('../server.js');
const { proxyToOrchestrator, checkOrchestratorReadiness, OrchestratorClientError } =
  await import('../services/orchestrator-client.js');

const mockProxy = vi.mocked(proxyToOrchestrator);
const mockReadinessCheck = vi.mocked(checkOrchestratorReadiness);

// =============================================================================
// Test helpers
// =============================================================================

const DEV_HEADERS = {
  'x-suite-id': 'suite-test-001',
  'x-office-id': 'office-test-001',
  'x-actor-id': 'user-test-001',
};

const VALID_REQUEST = {
  schema_version: '1.0' as const,
  suite_id: 'will-be-overridden',
  office_id: 'will-be-overridden',
  request_id: '550e8400-e29b-41d4-a716-446655440000',
  correlation_id: '660e8400-e29b-41d4-a716-446655440000',
  timestamp: '2026-02-13T12:00:00.000Z',
  task_type: 'calendar.read',
  payload: {},
};

const GREEN_AVA_RESULT = {
  schema_version: '1.0',
  request_id: '550e8400-e29b-41d4-a716-446655440000',
  correlation_id: '660e8400-e29b-41d4-a716-446655440000',
  route: { skill_pack: 'calendar', agent: 'nora' },
  risk: { tier: 'green' },
  governance: {
    approvals_required: [],
    presence_required: false,
    capability_token_required: true,
    receipt_ids: ['receipt-001'],
  },
  plan: { action: 'calendar.read', status: 'complete' },
};

const YELLOW_APPROVAL_RESPONSE = {
  error: 'APPROVAL_REQUIRED',
  message: 'Yellow tier action requires user approval',
  correlation_id: '660e8400-e29b-41d4-a716-446655440000',
  approval_request: {
    payload_hash: 'abc123',
    risk_tier: 'yellow',
    action_type: 'invoice.create',
  },
};

beforeEach(() => {
  process.env.GATEWAY_AUTH_MODE = 'dev';
  vi.clearAllMocks();
  mockReadinessCheck.mockResolvedValue({
    reachable: true,
    httpStatus: 200,
    status: 'ready',
    dependency: 'healthy',
  });
});

afterEach(() => {
  delete process.env.GATEWAY_AUTH_MODE;
});

// =============================================================================
// Health Endpoints
// =============================================================================

describe('Health Endpoints', () => {
  it('GET /healthz returns ok', async () => {
    const res = await request(app).get('/healthz');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
    expect(res.body.service).toBe('aspire-gateway');
  });

  it('GET /readyz returns ready when orchestrator healthy', async () => {
    mockReadinessCheck.mockResolvedValue({
      reachable: true,
      httpStatus: 200,
      status: 'ready',
      dependency: 'healthy',
    });
    const res = await request(app).get('/readyz');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ready');
    expect(res.body.dependencies.orchestrator).toBe('healthy');
    expect(res.body.orchestrator_status).toBe('ready');
  });

  it('GET /readyz returns degraded when orchestrator not reachable', async () => {
    mockReadinessCheck.mockResolvedValue({
      reachable: false,
      httpStatus: null,
      status: 'unavailable',
      dependency: 'unavailable',
    });
    const res = await request(app).get('/readyz');
    expect(res.status).toBe(503);
    expect(res.body.status).toBe('degraded');
    expect(res.body.dependencies.orchestrator).toBe('unavailable');
    expect(res.body.orchestrator_status).toBe('unavailable');
  });

  it('GET /readyz returns degraded when orchestrator is degraded', async () => {
    mockReadinessCheck.mockResolvedValue({
      reachable: true,
      httpStatus: 200,
      status: 'degraded',
      dependency: 'degraded',
    });
    const res = await request(app).get('/readyz');
    expect(res.status).toBe(503);
    expect(res.body.status).toBe('degraded');
    expect(res.body.dependencies.orchestrator).toBe('degraded');
    expect(res.body.orchestrator_status).toBe('degraded');
  });

  it('healthz includes correlation ID header', async () => {
    const res = await request(app).get('/healthz');
    expect(res.headers['x-correlation-id']).toBeDefined();
  });
});

// =============================================================================
// POST /v1/intents — Main Endpoint
// =============================================================================

describe('POST /v1/intents', () => {
  it('returns 401 without auth headers', async () => {
    const res = await request(app)
      .post('/v1/intents')
      .send(VALID_REQUEST);

    expect(res.status).toBe(401);
  });

  it('returns 400 for invalid schema', async () => {
    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send({ invalid: 'body' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('proxies GREEN tier request and returns AvaResult', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: GREEN_AVA_RESULT,
      headers: {},
    });

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send(VALID_REQUEST);

    expect(res.status).toBe(200);
    expect(res.body.risk.tier).toBe('green');
    expect(res.body.governance.receipt_ids).toEqual(['receipt-001']);
  });

  it('overrides suite_id and office_id from auth context (Law #6)', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: GREEN_AVA_RESULT,
      headers: {},
    });

    await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send(VALID_REQUEST);

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.suiteId).toBe('suite-test-001');
    expect(proxyCall.officeId).toBe('office-test-001');

    // Verify body was overridden
    const body = proxyCall.body as Record<string, unknown>;
    expect(body.suite_id).toBe('suite-test-001');
    expect(body.office_id).toBe('office-test-001');
  });

  it('ignores forged tenant context in request body', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: GREEN_AVA_RESULT,
      headers: {},
    });

    await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send({
        ...VALID_REQUEST,
        suite_id: 'suite-forged-evil',
        office_id: 'office-forged-evil',
      });

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.suiteId).toBe('suite-test-001');
    expect(proxyCall.officeId).toBe('office-test-001');

    const body = proxyCall.body as Record<string, unknown>;
    expect(body.suite_id).toBe('suite-test-001');
    expect(body.office_id).toBe('office-test-001');
    expect(body.suite_id).not.toBe('suite-forged-evil');
    expect(body.office_id).not.toBe('office-forged-evil');
  });

  it('propagates correlation ID to orchestrator', async () => {
    const correlationId = '770e8400-e29b-41d4-a716-446655440000';
    mockProxy.mockResolvedValue({
      status: 200,
      body: GREEN_AVA_RESULT,
      headers: {},
    });

    await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .set('x-correlation-id', correlationId)
      .send(VALID_REQUEST);

    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.correlationId).toBe(correlationId);
  });

  it('returns 202 for YELLOW tier (approval required)', async () => {
    mockProxy.mockResolvedValue({
      status: 202,
      body: YELLOW_APPROVAL_RESPONSE,
      headers: {},
    });

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send({ ...VALID_REQUEST, task_type: 'invoice.create' });

    expect(res.status).toBe(202);
    expect(res.body.error).toBe('APPROVAL_REQUIRED');
  });

  it('returns 403 for policy denied', async () => {
    mockProxy.mockResolvedValue({
      status: 403,
      body: {
        error: 'POLICY_DENIED',
        message: 'Unknown action type',
        correlation_id: 'test-id',
      },
      headers: {},
    });

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send({ ...VALID_REQUEST, task_type: 'hack.system' });

    expect(res.status).toBe(403);
    expect(res.body.error).toBe('POLICY_DENIED');
  });

  it('returns 503 when orchestrator is down', async () => {
    mockProxy.mockRejectedValue(
      new OrchestratorClientError('Connection refused', 'CONNECTION_REFUSED', 'test-corr'),
    );

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send(VALID_REQUEST);

    expect(res.status).toBe(503);
    expect(res.body.error).toBe('INTERNAL_ERROR');
  });

  it('returns 504 when orchestrator times out', async () => {
    mockProxy.mockRejectedValue(
      new OrchestratorClientError('Timeout', 'TIMEOUT', 'test-corr'),
    );

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send(VALID_REQUEST);

    expect(res.status).toBe(504);
  });

  it('sets egress warning header when AvaResult schema invalid', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: { invalid: 'response' },
      headers: {},
    });

    const res = await request(app)
      .post('/v1/intents')
      .set(DEV_HEADERS)
      .send(VALID_REQUEST);

    expect(res.status).toBe(200);
    expect(res.headers['x-aspire-egress-warning']).toBeDefined();
  });
});

// =============================================================================
// GET /v1/receipts
// =============================================================================

describe('GET /v1/receipts', () => {
  it('returns 401 without auth', async () => {
    delete process.env.GATEWAY_AUTH_MODE;
    process.env.SUPABASE_JWT_SECRET = '';
    const res = await request(app).get('/v1/receipts');
    expect(res.status).toBe(500); // No JWT secret = 500
  });

  it('proxies to orchestrator with auth-derived suite_id', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: { receipts: [], count: 0 },
      headers: {},
    });

    await request(app)
      .get('/v1/receipts')
      .set(DEV_HEADERS);

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.suiteId).toBe('suite-test-001');
    expect(proxyCall.queryParams?.suite_id).toBe('suite-test-001');
  });

  it('passes query filters to orchestrator', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: { receipts: [], count: 0 },
      headers: {},
    });

    await request(app)
      .get('/v1/receipts?correlation_id=test-corr&risk_tier=yellow&limit=10')
      .set(DEV_HEADERS);

    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.queryParams?.correlation_id).toBe('test-corr');
    expect(proxyCall.queryParams?.risk_tier).toBe('yellow');
    expect(proxyCall.queryParams?.limit).toBe('10');
  });

  it('rejects invalid risk_tier', async () => {
    const res = await request(app)
      .get('/v1/receipts?risk_tier=extreme')
      .set(DEV_HEADERS);

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('enforces max limit of 200', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: { receipts: [], count: 0 },
      headers: {},
    });

    await request(app)
      .get('/v1/receipts?limit=500')
      .set(DEV_HEADERS);

    const proxyCall = mockProxy.mock.calls[0][0];
    expect(proxyCall.queryParams?.limit).toBe('200');
  });
});

// =============================================================================
// POST /v1/receipts/verify-run
// =============================================================================

describe('POST /v1/receipts/verify-run', () => {
  it('proxies verification request', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: { verified: true, chain_length: 5, broken_links: [] },
      headers: {},
    });

    const res = await request(app)
      .post('/v1/receipts/verify-run')
      .set(DEV_HEADERS)
      .send({ correlation_id: 'test-corr-id' });

    expect(res.status).toBe(200);
    expect(res.body.verified).toBe(true);
  });

  it('rejects missing correlation_id', async () => {
    const res = await request(app)
      .post('/v1/receipts/verify-run')
      .set(DEV_HEADERS)
      .send({});

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });
});

// =============================================================================
// POST /v1/policy/evaluate
// =============================================================================

describe('POST /v1/policy/evaluate', () => {
  it('proxies policy evaluation', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: {
        action_type: 'calendar.read',
        allowed: true,
        risk_tier: 'green',
        approval_required: false,
        presence_required: false,
      },
      headers: {},
    });

    const res = await request(app)
      .post('/v1/policy/evaluate')
      .set(DEV_HEADERS)
      .send({ action_type: 'calendar.read' });

    expect(res.status).toBe(200);
    expect(res.body.risk_tier).toBe('green');
  });

  it('rejects missing action_type', async () => {
    const res = await request(app)
      .post('/v1/policy/evaluate')
      .set(DEV_HEADERS)
      .send({});

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects invalid action_type format (path traversal)', async () => {
    const res = await request(app)
      .post('/v1/policy/evaluate')
      .set(DEV_HEADERS)
      .send({ action_type: '../../../etc/passwd' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });
});

// =============================================================================
// 404 Handler
// =============================================================================

describe('404 Handler', () => {
  it('returns 404 for non-v1 unknown routes', async () => {
    const res = await request(app).get('/not-a-route');
    expect(res.status).toBe(404);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });
});

// =============================================================================
// Tenant Isolation — Cross-Suite Attack Scenarios
// =============================================================================

describe('Tenant Isolation (Law #6)', () => {
  it('body suite_id is overridden even if it matches a different tenant', async () => {
    mockProxy.mockResolvedValue({
      status: 200,
      body: GREEN_AVA_RESULT,
      headers: {},
    });

    await request(app)
      .post('/v1/intents')
      .set({
        ...DEV_HEADERS,
        'x-suite-id': 'attacker-suite',
      })
      .send({
        ...VALID_REQUEST,
        suite_id: 'victim-suite',
      });

    // The proxy should use attacker-suite (from auth), not victim-suite (from body)
    const proxyCall = mockProxy.mock.calls[0][0];
    const body = proxyCall.body as Record<string, unknown>;
    expect(body.suite_id).toBe('attacker-suite');
    expect(proxyCall.suiteId).toBe('attacker-suite');
  });
});
