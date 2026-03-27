/**
 * Aspire Gateway API Server — Wave 6 Complete
 *
 * TypeScript/Express gateway in front of the Python LangGraph orchestrator.
 *
 * Middleware stack (order matters):
 * 1. helmet() — Security headers
 * 2. cors() — Cross-origin resource sharing
 * 3. express.json() — Body parsing (1MB limit)
 * 4. correlationIdMiddleware — Distributed tracing (Gate 2)
 * 5. authMiddleware — JWT/dev auth, suite_id derivation (Law #6)
 * 6. Rate limiters — Per-suite abuse prevention (Gate 5)
 *
 * Routes:
 * - POST /v1/intents — Main orchestrator endpoint (proxied)
 * - GET  /v1/receipts — Receipt query (proxied)
 * - POST /v1/receipts/verify-run — Hash chain verification (proxied)
 * - POST /v1/policy/evaluate — Policy evaluation (proxied)
 * - GET  /v1/registry/* — Capability discovery (proxied)
 * - POST /v1/a2a/* — Agent-to-agent task routing (proxied)
 *
 * Health/readiness:
 * - GET /healthz — Liveness probe
 * - GET /readyz — Readiness probe (includes orchestrator check)
 */

import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import { correlationIdMiddleware } from './middleware/correlation-id.js';
import { authMiddleware } from './middleware/auth.js';
import { schemaValidationMiddleware } from './middleware/schema-validation.js';
import { standardRateLimiter, elevatedRateLimiter } from './middleware/rate-limit.js';
import { intentsRouter } from './routes/intents.js';
import { receiptsRouter } from './routes/receipts.js';
import { policyRouter } from './routes/policy.js';
import { registryRouter } from './routes/registry.js';
import { a2aRouter } from './routes/a2a.js';
import { elevenlabsToolsRouter } from './routes/elevenlabs-tools.js';
import { elevenlabsWebhooksRouter } from './routes/elevenlabs-webhooks.js';
import { elevenlabsToolAuthMiddleware } from './middleware/elevenlabs-auth.js';
import { 
  checkOrchestratorReadiness,
  proxyToOrchestrator 
} from './services/orchestrator-client.js';
import { logger } from './services/logger.js';

const app = express();
const PORT = process.env.GATEWAY_PORT ? parseInt(process.env.GATEWAY_PORT, 10) : 3100;

// =============================================================================
// Global Middleware (applied to ALL requests)
// =============================================================================

// Security headers
app.use(helmet());

// CORS — restricted to production domain + local dev ports (Law #9)
app.use(cors({
  origin: process.env.ALLOWED_ORIGINS?.split(',') || [
    'https://www.aspireos.app',
    'http://localhost:8080',
    'http://localhost:5000',
    'http://localhost:19006',
  ],
  credentials: true,
}));

// Body parsing with size limit
app.use(express.json({ limit: '1mb' }));

// Correlation ID for distributed tracing (Gate 2: Observability)
app.use(correlationIdMiddleware);

// =============================================================================
// Health & Readiness (NO auth required)
// =============================================================================

app.get('/healthz', (_req, res) => {
  res.json({
    status: 'ok',
    service: 'aspire-gateway',
    version: '0.1.0',
    timestamp: new Date().toISOString(),
  });
});

app.get('/readyz', async (_req, res) => {
  const orchestrator = await checkOrchestratorReadiness();
  const status = orchestrator.dependency === 'healthy' ? 'ready' : 'degraded';
  const statusCode = orchestrator.dependency === 'healthy' ? 200 : 503;

  res.status(statusCode).json({
    status,
    service: 'aspire-gateway',
    dependencies: {
      orchestrator: orchestrator.dependency,
    },
    orchestrator_status: orchestrator.status,
  });
});

// =============================================================================
// ElevenLabs Agent Tool Endpoints (shared secret auth, no JWT)
// =============================================================================

// POST /v1/tools/* — ElevenLabs server tools call these endpoints
app.use('/v1/tools', standardRateLimiter, elevenlabsToolAuthMiddleware, elevenlabsToolsRouter);

// =============================================================================
// ElevenLabs Webhooks (HMAC signature verification, no JWT)
// =============================================================================

// POST /v1/webhooks/elevenlabs/* — Post-call transcript ingestion
app.use('/v1/webhooks/elevenlabs', standardRateLimiter, elevenlabsWebhooksRouter);

// =============================================================================
// Webhooks (NO JWT auth — use provider signatures)
// =============================================================================

// POST /api/webhooks/* — Direct proxy to orchestrator
app.post('/api/webhooks/*', standardRateLimiter, async (req, res) => {
  const correlationId = req.correlationId;
  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: req.path,
      method: 'POST',
      body: req.body,
      correlationId,
      suiteId: 'system', // Webhooks are system-level ingress
      officeId: 'system',
      actorId: 'webhook_ingress',
    });
    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    logger.error('Gateway webhook proxy error', { 
      correlation_id: correlationId, 
      error: err instanceof Error ? err.message : String(err) 
    });
    res.status(502).json({ error: 'INTERNAL_ERROR', message: 'Failed to proxy webhook' });
  }
});

// =============================================================================
// Authenticated Routes (auth middleware applied)
// =============================================================================

// Auth middleware for all /v1/* and /admin/* routes
app.use(['/v1', '/admin'], authMiddleware);

// Admin routes — Direct proxy to orchestrator
app.all('/admin/*', elevatedRateLimiter, async (req, res) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  
  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: req.path,
      method: req.method as any,
      body: req.method !== 'GET' ? req.body : undefined,
      correlationId,
      suiteId,
      officeId,
      actorId,
    });
    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    logger.error('Gateway admin proxy error', { 
      correlation_id: correlationId, 
      error: err instanceof Error ? err.message : String(err) 
    });
    res.status(502).json({ error: 'INTERNAL_ERROR', message: 'Failed to proxy admin request' });
  }
});

// POST /v1/sessions/signed-url — ElevenLabs session creation (requires JWT)
// SECURITY: Only expose /signed-url at this mount point. Do NOT mount the full
// elevenlabsToolsRouter here — that would expose /execute, /draft, /approve
// to any JWT-authenticated user without the tool-secret gate (THREAT-002 fix).
import { elevenlabsSessionsRouter } from './routes/elevenlabs-sessions.js';
app.use('/v1/sessions', standardRateLimiter, elevenlabsSessionsRouter);

// POST /v1/intents — Main endpoint
app.use('/v1/intents', standardRateLimiter, schemaValidationMiddleware, intentsRouter);

// GET /v1/receipts + POST /v1/receipts/verify-run
// Elevated rate limit (200/min — read-heavy)
app.use('/v1/receipts', elevatedRateLimiter, receiptsRouter);

// POST /v1/policy/evaluate
// Elevated rate limit (200/min — lightweight, no execution)
app.use('/v1/policy', elevatedRateLimiter, policyRouter);

// GET /v1/registry/* — Capability discovery
// Elevated rate limit (200/min — read-only discovery)
app.use('/v1/registry', elevatedRateLimiter, registryRouter);

// POST /v1/a2a/* — Agent-to-agent task routing
// Standard rate limit (100/min — state-changing operations)
app.use('/v1/a2a', standardRateLimiter, a2aRouter);

// =============================================================================
// 404 handler for unknown routes
// =============================================================================

app.use((_req, res) => {
  res.status(404).json({
    error: 'SCHEMA_VALIDATION_FAILED',
    message: 'Route not found',
    correlation_id: _req.correlationId ?? 'unknown',
  });
});

// =============================================================================
// Global error handler (last resort — fail-closed, Law #3)
// =============================================================================

app.use((err: Error, req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error('[GATEWAY ERROR]', {
    error: err.message,
    correlationId: req.correlationId,
    path: req.path,
    method: req.method,
  });

  res.status(500).json({
    error: 'INTERNAL_ERROR',
    message: 'An unexpected error occurred',
    correlation_id: req.correlationId ?? 'unknown',
  });
});

// =============================================================================
// Server startup
// =============================================================================

if (process.env.NODE_ENV !== 'test') {
  const orchestratorTarget = process.env.ORCHESTRATOR_URL?.trim() || 'http://localhost:8000';
  app.listen(PORT, () => {
    console.log(`Aspire Gateway listening on port ${PORT}`);
    console.log(`  Auth mode: ${process.env.GATEWAY_AUTH_MODE ?? 'production'}`);
    console.log(`  Orchestrator: ${orchestratorTarget}`);
  });
}

export { app };
