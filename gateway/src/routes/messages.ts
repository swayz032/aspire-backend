/**
 * Messages Routes — Pass 19 Lane E1.
 *
 * Thin proxy router: forwards all /api/messages/* requests to the
 * orchestrator /v1/messages/* endpoints.
 *
 * Endpoints proxied:
 *   GET  /api/messages/threads                         → /v1/messages/threads
 *   GET  /api/messages/threads/:threadId/messages      → /v1/messages/threads/:threadId/messages
 *   PATCH /api/messages/threads/:threadId/read         → /v1/messages/threads/:threadId/read
 *   PATCH /api/messages/threads/:threadId/pin          → /v1/messages/threads/:threadId/pin
 *   PATCH /api/messages/threads/:threadId/archive      → /v1/messages/threads/:threadId/archive
 *   GET  /api/messages/contacts/search                 → /v1/messages/contacts/search
 *   GET  /api/messages/templates                       → /v1/messages/templates
 *   GET  /api/messages/suggestions                     → /v1/messages/suggestions
 *
 * Auth: JWT-authenticated (authMiddleware applied at mount in server.ts).
 * Tenant context forwarded via X-Suite-Id, X-Office-Id, X-Tenant-Id headers.
 *
 * Law compliance:
 *   Law #3 — fail-closed: proxy errors return structured errors to caller.
 *   Law #6 — tenant isolation: suite_id/office_id from auth, forwarded as headers.
 *   Law #9 — no PII logged (phone numbers, message bodies not logged here).
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { logger } from '../services/logger.js';

export const messagesRouter = Router();

// ---------------------------------------------------------------------------
// Shared proxy helper for this router
// ---------------------------------------------------------------------------

async function proxyMessages(
  req: Request,
  res: Response,
  orchestratorPath: string,
  overrideMethod?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH',
): Promise<void> {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  // Forward query params as-is (includes capability_token, filter, limit, cursor, etc.)
  const queryParams: Record<string, string> = {};
  for (const [key, val] of Object.entries(req.query)) {
    if (typeof val === 'string') {
      queryParams[key] = val;
    }
  }

  // Map Express req.method to the expected type union
  const methodMap: Record<string, 'GET' | 'POST' | 'PUT' | 'DELETE'> = {
    GET: 'GET',
    POST: 'POST',
    PUT: 'PUT',
    DELETE: 'DELETE',
    PATCH: 'POST',  // proxyToOrchestrator doesn't have PATCH; use POST body passthrough
  };

  // For PATCH, we send body as POST to the orchestrator (orchestrator FastAPI router
  // accepts PATCH method directly — we forward the HTTP method via a custom header
  // so the orchestrator can route correctly).
  // Actually: proxyToOrchestrator only supports GET/POST/PUT/DELETE. For PATCH we
  // forward as the same method using fetch directly to avoid signature mismatch.
  // We use a direct fetch approach mirroring proxyToOrchestrator internals.

  const requestedMethod = overrideMethod ?? (req.method as 'GET' | 'POST' | 'PUT' | 'DELETE');

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: orchestratorPath,
      method: requestedMethod === ('PATCH' as string) ? 'POST' : requestedMethod,
      body: ['POST', 'PUT', 'PATCH'].includes(req.method) ? req.body : undefined,
      correlationId,
      suiteId,
      officeId,
      actorId,
      queryParams: Object.keys(queryParams).length > 0 ? queryParams : undefined,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleMessagesError(err, res, correlationId, suiteId, orchestratorPath);
  }
}

// For PATCH endpoints we need a separate approach since proxyToOrchestrator
// only accepts GET/POST/PUT/DELETE. We call the orchestrator with the PATCH
// method by using fetch directly.
const ORCHESTRATOR_BASE_URL =
  (process.env.ORCHESTRATOR_URL?.trim()) || 'http://localhost:8000';

async function patchToOrchestrator(
  req: Request,
  res: Response,
  orchestratorPath: string,
): Promise<void> {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const url = `${ORCHESTRATOR_BASE_URL}${orchestratorPath}`;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Correlation-Id': correlationId,
    'X-Suite-Id': suiteId,
    'X-Office-Id': officeId,
    'X-Actor-Id': actorId,
  };

  // Forward tenant ID from auth context (Law #6)
  if (req.headers['x-tenant-id']) {
    headers['X-Tenant-Id'] = req.headers['x-tenant-id'] as string;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000);

  try {
    const response = await fetch(url, {
      method: 'PATCH',
      headers,
      body: JSON.stringify(req.body ?? {}),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    let responseBody: unknown;
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      responseBody = await response.json();
    } else {
      responseBody = await response.text();
    }

    res.status(response.status).json(responseBody);
  } catch (err) {
    clearTimeout(timeoutId);
    handleMessagesError(err, res, correlationId, suiteId, orchestratorPath);
  }
}

// ---------------------------------------------------------------------------
// GET /api/messages/threads
// ---------------------------------------------------------------------------
messagesRouter.get('/threads', async (req: Request, res: Response) => {
  await proxyMessages(req, res, '/v1/messages/threads', 'GET');
});

// ---------------------------------------------------------------------------
// GET /api/messages/threads/:threadId/messages
// ---------------------------------------------------------------------------
messagesRouter.get('/threads/:threadId/messages', async (req: Request, res: Response) => {
  const { threadId } = req.params;
  await proxyMessages(req, res, `/v1/messages/threads/${threadId}/messages`, 'GET');
});

// ---------------------------------------------------------------------------
// PATCH /api/messages/threads/:threadId/read
// ---------------------------------------------------------------------------
messagesRouter.patch('/threads/:threadId/read', async (req: Request, res: Response) => {
  const { threadId } = req.params;
  await patchToOrchestrator(req, res, `/v1/messages/threads/${threadId}/read`);
});

// ---------------------------------------------------------------------------
// PATCH /api/messages/threads/:threadId/pin
// ---------------------------------------------------------------------------
messagesRouter.patch('/threads/:threadId/pin', async (req: Request, res: Response) => {
  const { threadId } = req.params;
  await patchToOrchestrator(req, res, `/v1/messages/threads/${threadId}/pin`);
});

// ---------------------------------------------------------------------------
// PATCH /api/messages/threads/:threadId/archive
// ---------------------------------------------------------------------------
messagesRouter.patch('/threads/:threadId/archive', async (req: Request, res: Response) => {
  const { threadId } = req.params;
  await patchToOrchestrator(req, res, `/v1/messages/threads/${threadId}/archive`);
});

// ---------------------------------------------------------------------------
// GET /api/messages/contacts/search
// ---------------------------------------------------------------------------
messagesRouter.get('/contacts/search', async (req: Request, res: Response) => {
  await proxyMessages(req, res, '/v1/messages/contacts/search', 'GET');
});

// ---------------------------------------------------------------------------
// GET /api/messages/templates
// ---------------------------------------------------------------------------
messagesRouter.get('/templates', async (req: Request, res: Response) => {
  await proxyMessages(req, res, '/v1/messages/templates', 'GET');
});

// ---------------------------------------------------------------------------
// GET /api/messages/suggestions
// ---------------------------------------------------------------------------
messagesRouter.get('/suggestions', async (req: Request, res: Response) => {
  await proxyMessages(req, res, '/v1/messages/suggestions', 'GET');
});

// ---------------------------------------------------------------------------
// Shared error handler
// ---------------------------------------------------------------------------
function handleMessagesError(
  err: unknown,
  res: Response,
  correlationId: string,
  suiteId: string,
  endpoint: string,
): void {
  if (err instanceof OrchestratorClientError) {
    const statusMap: Record<string, number> = {
      TIMEOUT: 504,
      CONNECTION_REFUSED: 503,
      INVALID_RESPONSE: 502,
      UNKNOWN: 500,
    };
    const mappedStatus = statusMap[err.code] ?? 500;

    logger.error('Messages proxy orchestrator failure', {
      correlation_id: correlationId,
      suite_id: suiteId.substring(0, 8),
      endpoint,
      error_code: err.code,
      status: mappedStatus,
    });

    res.status(mappedStatus).json({
      error: 'INTERNAL_ERROR',
      message: err.message,
      correlation_id: correlationId,
    });
    return;
  }

  if (err instanceof Error && err.name === 'AbortError') {
    logger.error('Messages proxy timeout', {
      correlation_id: correlationId,
      suite_id: suiteId.substring(0, 8),
      endpoint,
    });
    res.status(504).json({
      error: 'GATEWAY_TIMEOUT',
      message: 'Orchestrator did not respond in time',
      correlation_id: correlationId,
    });
    return;
  }

  logger.error('Messages proxy unexpected error', {
    correlation_id: correlationId,
    suite_id: suiteId.substring(0, 8),
    endpoint,
    error: err instanceof Error ? err.message : String(err),
  });

  res.status(500).json({
    error: 'INTERNAL_ERROR',
    message: err instanceof Error ? err.message : 'Unknown error',
    correlation_id: correlationId,
  });
}
