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
import { checkOrchestratorHealth } from './services/orchestrator-client.js';

const app = express();
const PORT = process.env.GATEWAY_PORT ? parseInt(process.env.GATEWAY_PORT, 10) : 3100;

// =============================================================================
// Global Middleware (applied to ALL requests)
// =============================================================================

// Security headers
app.use(helmet());

// CORS (will be restricted to specific origins in production)
app.use(cors());

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
  const orchestratorHealthy = await checkOrchestratorHealth();
  const status = orchestratorHealthy ? 'ready' : 'degraded';
  const statusCode = orchestratorHealthy ? 200 : 503;

  res.status(statusCode).json({
    status,
    service: 'aspire-gateway',
    dependencies: {
      orchestrator: orchestratorHealthy ? 'healthy' : 'unavailable',
    },
  });
});

// =============================================================================
// Authenticated Routes (auth middleware applied)
// =============================================================================

// Auth middleware for all /v1/* routes
app.use('/v1', authMiddleware);

// POST /v1/intents — Main endpoint
// Schema validation middleware applied only to intents (strict body validation)
// Standard rate limit (100/min per suite)
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
  app.listen(PORT, () => {
    console.log(`Aspire Gateway listening on port ${PORT}`);
    console.log(`  Auth mode: ${process.env.GATEWAY_AUTH_MODE ?? 'production'}`);
    console.log(`  Orchestrator: ${process.env.ORCHESTRATOR_URL ?? 'http://localhost:8000'}`);
  });
}

export { app };
