/**
 * Gateway Middleware Tests — Auth, Correlation ID, Schema Validation
 *
 * Tests cover:
 * - Correlation ID generation and propagation
 * - Auth middleware (dev mode): header extraction, missing headers, invalid types
 * - Schema validation: valid/invalid AvaOrchestratorRequest bodies
 * - Egress validation: AvaResult schema checking
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import express from 'express';
import request from 'supertest';
import { correlationIdMiddleware } from '../middleware/correlation-id.js';
import { authMiddleware } from '../middleware/auth.js';
import { schemaValidationMiddleware, validateAvaResult } from '../middleware/schema-validation.js';

// =============================================================================
// Test helper: create minimal Express app with specific middleware
// =============================================================================

function createTestApp(...middlewares: express.RequestHandler[]) {
  const app = express();
  app.use(express.json());
  for (const mw of middlewares) {
    app.use(mw);
  }
  // Echo route that returns middleware-injected values
  app.post('/echo', (req, res) => {
    res.json({
      correlationId: req.correlationId,
      auth: req.auth,
      body: req.body,
    });
  });
  app.get('/echo', (req, res) => {
    res.json({
      correlationId: req.correlationId,
      auth: req.auth,
    });
  });
  return app;
}

// =============================================================================
// Correlation ID Middleware Tests
// =============================================================================

describe('Correlation ID Middleware', () => {
  const app = createTestApp(correlationIdMiddleware);

  it('generates UUID when no header present', async () => {
    const res = await request(app).get('/echo');
    expect(res.status).toBe(200);
    expect(res.body.correlationId).toBeDefined();
    // UUID v4 format
    expect(res.body.correlationId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });

  it('propagates existing correlation ID header', async () => {
    const knownId = '550e8400-e29b-41d4-a716-446655440000';
    const res = await request(app)
      .get('/echo')
      .set('x-correlation-id', knownId);
    expect(res.body.correlationId).toBe(knownId);
  });

  it('sets correlation ID on response header', async () => {
    const res = await request(app).get('/echo');
    expect(res.headers['x-correlation-id']).toBeDefined();
    expect(res.headers['x-correlation-id']).toBe(res.body.correlationId);
  });

  it('ignores empty correlation ID header', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-correlation-id', '');
    expect(res.body.correlationId).toBeDefined();
    // Should generate a new one, not use empty string
    expect(res.body.correlationId.length).toBeGreaterThan(0);
  });
});

// =============================================================================
// Auth Middleware Tests (Dev Mode)
// =============================================================================

describe('Auth Middleware (dev mode)', () => {
  // Set dev mode for all auth tests
  beforeEach(() => {
    vi.stubEnv('GATEWAY_AUTH_MODE', 'dev');
  });

  const app = createTestApp(correlationIdMiddleware, authMiddleware);

  it('extracts auth context from dev headers', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123')
      .set('x-office-id', 'office-456')
      .set('x-actor-id', 'user-789');

    expect(res.status).toBe(200);
    expect(res.body.auth).toEqual({
      suiteId: 'suite-123',
      officeId: 'office-456',
      actorId: 'user-789',
      actorType: 'user',
    });
  });

  it('returns 401 when x-suite-id missing', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-office-id', 'office-456');

    expect(res.status).toBe(401);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(res.body.message).toContain('x-suite-id');
  });

  it('returns 401 when x-office-id missing', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123');

    expect(res.status).toBe(401);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
    expect(res.body.message).toContain('x-office-id');
  });

  it('defaults actor_id to dev-user when not provided', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123')
      .set('x-office-id', 'office-456');

    expect(res.status).toBe(200);
    expect(res.body.auth.actorId).toBe('dev-user');
  });

  it('defaults actor_type to user', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123')
      .set('x-office-id', 'office-456');

    expect(res.status).toBe(200);
    expect(res.body.auth.actorType).toBe('user');
  });

  it('accepts custom actor_type', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123')
      .set('x-office-id', 'office-456')
      .set('x-actor-type', 'system');

    expect(res.status).toBe(200);
    expect(res.body.auth.actorType).toBe('system');
  });

  it('rejects invalid actor_type', async () => {
    const res = await request(app)
      .get('/echo')
      .set('x-suite-id', 'suite-123')
      .set('x-office-id', 'office-456')
      .set('x-actor-type', 'hacker');

    expect(res.status).toBe(401);
    expect(res.body.message).toContain('Invalid x-actor-type');
  });

  it('includes correlation_id in error response', async () => {
    const correlationId = '550e8400-e29b-41d4-a716-446655440000';
    const res = await request(app)
      .get('/echo')
      .set('x-correlation-id', correlationId);

    expect(res.status).toBe(401);
    expect(res.body.correlation_id).toBe(correlationId);
  });
});

// =============================================================================
// Auth Middleware Tests (Production Mode — JWT)
// =============================================================================

describe('Auth Middleware (production mode)', () => {
  beforeEach(() => {
    vi.stubEnv('GATEWAY_AUTH_MODE', 'production');
  });

  const app = createTestApp(correlationIdMiddleware, authMiddleware);

  it('returns 500 when JWT_SECRET not configured', async () => {
    vi.stubEnv('SUPABASE_JWT_SECRET', '');
    const res = await request(app)
      .get('/echo')
      .set('Authorization', 'Bearer fake-token');

    expect(res.status).toBe(500);
    expect(res.body.error).toBe('INTERNAL_ERROR');
    expect(res.body.message).toContain('JWT secret not configured');
  });

  it('returns 401 when no Authorization header', async () => {
    vi.stubEnv('SUPABASE_JWT_SECRET', 'test-secret-key-for-jwt');
    const res = await request(app).get('/echo');

    expect(res.status).toBe(401);
    expect(res.body.message).toContain('Missing or malformed Authorization');
  });

  it('returns 401 for malformed Authorization header', async () => {
    vi.stubEnv('SUPABASE_JWT_SECRET', 'test-secret-key-for-jwt');
    const res = await request(app)
      .get('/echo')
      .set('Authorization', 'Basic dXNlcjpwYXNz');

    expect(res.status).toBe(401);
    expect(res.body.message).toContain('Missing or malformed');
  });

  it('returns 401 for invalid JWT', async () => {
    vi.stubEnv('SUPABASE_JWT_SECRET', 'test-secret-key-for-jwt');
    const res = await request(app)
      .get('/echo')
      .set('Authorization', 'Bearer invalid.jwt.token');

    expect(res.status).toBe(401);
    expect(res.body.message).toContain('Authentication failed');
  });
});

// =============================================================================
// Schema Validation Middleware Tests
// =============================================================================

describe('Schema Validation Middleware', () => {
  const app = createTestApp(correlationIdMiddleware, schemaValidationMiddleware);

  const validRequest = {
    schema_version: '1.0',
    suite_id: 'suite-123',
    office_id: 'office-456',
    request_id: '550e8400-e29b-41d4-a716-446655440000',
    correlation_id: '660e8400-e29b-41d4-a716-446655440000',
    timestamp: '2026-02-13T12:00:00.000Z',
    task_type: 'calendar.read',
    payload: {},
  };

  it('passes valid AvaOrchestratorRequest', async () => {
    const res = await request(app)
      .post('/echo')
      .send(validRequest);

    expect(res.status).toBe(200);
    expect(res.body.body.task_type).toBe('calendar.read');
  });

  it('rejects missing schema_version', async () => {
    const { schema_version: _, ...invalid } = validRequest;
    const res = await request(app).post('/echo').send(invalid);

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects wrong schema_version', async () => {
    const res = await request(app)
      .post('/echo')
      .send({ ...validRequest, schema_version: '2.0' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects missing task_type', async () => {
    const { task_type: _, ...invalid } = validRequest;
    const res = await request(app).post('/echo').send(invalid);

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects invalid request_id format', async () => {
    const res = await request(app)
      .post('/echo')
      .send({ ...validRequest, request_id: 'not-a-uuid' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects invalid timestamp format', async () => {
    const res = await request(app)
      .post('/echo')
      .send({ ...validRequest, timestamp: 'not-a-date' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects payload that is not an object', async () => {
    const res = await request(app)
      .post('/echo')
      .send({ ...validRequest, payload: 'string-payload' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects additional properties', async () => {
    const res = await request(app)
      .post('/echo')
      .send({ ...validRequest, hacker_field: 'injected' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('rejects empty body', async () => {
    const res = await request(app)
      .post('/echo')
      .set('Content-Type', 'application/json')
      .send('');

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('SCHEMA_VALIDATION_FAILED');
  });

  it('includes correlation_id in error response', async () => {
    const correlationId = '550e8400-e29b-41d4-a716-446655440000';
    const res = await request(app)
      .post('/echo')
      .set('x-correlation-id', correlationId)
      .send({});

    expect(res.status).toBe(400);
    expect(res.body.correlation_id).toBe(correlationId);
  });
});

// =============================================================================
// AvaResult Egress Validation Tests
// =============================================================================

describe('AvaResult Egress Validation', () => {
  const validResult = {
    schema_version: '1.0',
    request_id: '550e8400-e29b-41d4-a716-446655440000',
    correlation_id: '660e8400-e29b-41d4-a716-446655440000',
    route: { skill_pack: 'calendar' },
    risk: { tier: 'green' },
    governance: {
      approvals_required: [],
      presence_required: false,
      capability_token_required: true,
      receipt_ids: ['receipt-1'],
    },
    plan: {},
  };

  it('validates correct AvaResult', () => {
    const result = validateAvaResult(validResult);
    expect(result.valid).toBe(true);
  });

  it('rejects missing governance', () => {
    const { governance: _, ...invalid } = validResult;
    const result = validateAvaResult(invalid);
    expect(result.valid).toBe(false);
    expect(result.errors).toContain('governance');
  });

  it('rejects invalid risk tier', () => {
    const invalid = { ...validResult, risk: { tier: 'extreme' } };
    const result = validateAvaResult(invalid);
    expect(result.valid).toBe(false);
  });

  it('rejects missing request_id', () => {
    const { request_id: _, ...invalid } = validResult;
    const result = validateAvaResult(invalid);
    expect(result.valid).toBe(false);
  });
});
