/**
 * Receipt Route Handlers — GET /v1/receipts + POST /v1/receipts/verify-run
 *
 * Law #2: Every action produces a receipt. These endpoints allow querying
 * and verifying the immutable audit trail.
 *
 * GET /v1/receipts — Query receipts for the authenticated suite (RLS-scoped)
 * POST /v1/receipts/verify-run — Trigger hash chain verification for a correlation_id
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';

export const receiptsRouter = Router();

/**
 * GET /v1/receipts — Query receipts for the authenticated suite.
 *
 * Query params:
 *   correlation_id — Filter by correlation ID
 *   action_type — Filter by action type (e.g., "invoice.create")
 *   risk_tier — Filter by risk tier (green|yellow|red)
 *   limit — Max results (default 50, max 200)
 *   offset — Pagination offset
 */
receiptsRouter.get('/', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  // Build query params, enforcing limits
  const queryParams: Record<string, string> = {};
  const { correlation_id, action_type, risk_tier, limit, offset } = req.query;

  if (typeof correlation_id === 'string') queryParams.correlation_id = correlation_id;
  if (typeof action_type === 'string') queryParams.action_type = action_type;
  if (typeof risk_tier === 'string') {
    if (!['green', 'yellow', 'red'].includes(risk_tier)) {
      res.status(400).json({
        error: 'SCHEMA_VALIDATION_FAILED',
        message: `Invalid risk_tier: ${risk_tier}. Must be green, yellow, or red.`,
        correlation_id: correlationId,
      });
      return;
    }
    queryParams.risk_tier = risk_tier;
  }

  // Enforce max limit of 200
  const parsedLimit = Math.min(parseInt(String(limit ?? '50'), 10) || 50, 200);
  queryParams.limit = String(parsedLimit);

  const parsedOffset = Math.max(parseInt(String(offset ?? '0'), 10) || 0, 0);
  queryParams.offset = String(parsedOffset);

  // suite_id is always derived from auth — tenant isolation
  queryParams.suite_id = suiteId;

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/receipts',
      method: 'GET',
      correlationId,
      suiteId,
      officeId,
      actorId,
      queryParams,
    });

    res.status(response.status).json(response.body);
  } catch (err) {
    handleProxyError(err, correlationId, res);
  }
});

/**
 * POST /v1/receipts/verify-run — Verify receipt hash chain for a correlation_id.
 *
 * Returns verification result: { verified, chain_length, broken_links }
 */
receiptsRouter.post('/verify-run', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { correlation_id: targetCorrelationId } = req.body ?? {};
  if (typeof targetCorrelationId !== 'string' || targetCorrelationId.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Request body must include correlation_id (string)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/receipts/verify-run',
      method: 'POST',
      body: {
        correlation_id: targetCorrelationId,
        suite_id: suiteId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(response.status).json(response.body);
  } catch (err) {
    handleProxyError(err, correlationId, res);
  }
});

function handleProxyError(err: unknown, correlationId: string, res: Response): void {
  if (err instanceof OrchestratorClientError) {
    const statusMap: Record<string, number> = {
      TIMEOUT: 504,
      CONNECTION_REFUSED: 503,
      INVALID_RESPONSE: 502,
      UNKNOWN: 500,
    };
    res.status(statusMap[err.code] ?? 500).json({
      error: 'INTERNAL_ERROR',
      message: err.message,
      correlation_id: correlationId,
    });
    return;
  }
  res.status(500).json({
    error: 'INTERNAL_ERROR',
    message: err instanceof Error ? err.message : 'Unknown error',
    correlation_id: correlationId,
  });
}
