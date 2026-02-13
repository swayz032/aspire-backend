/**
 * Intent Route Handler — POST /v1/intents (Primary Gateway Endpoint)
 *
 * This is the main entry point for all Aspire actions. The flow:
 * 1. Auth middleware has derived suite_id from JWT (not client body)
 * 2. Schema validation has validated AvaOrchestratorRequest
 * 3. Override suite_id/office_id with auth-derived values (Law #6)
 * 4. Forward to Python orchestrator via HTTP bridge
 * 5. Egress validate AvaResult before returning to client
 *
 * Governance: Every request flows through the full pipeline:
 * Intent → Context → Plan → Policy → Approval → Execute → Receipt → Summary
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { validateAvaResult } from '../middleware/schema-validation.js';

export const intentsRouter = Router();

intentsRouter.post('/', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  // CRITICAL: Override suite_id and office_id from auth context.
  // Per architecture.md: "The orchestrator derives the authoritative suite_id
  // from JWT, NOT from this payload."
  const orchestratorPayload = {
    ...req.body,
    suite_id: suiteId,
    office_id: officeId,
    correlation_id: correlationId,
  };

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: orchestratorPayload,
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    const responseBody = orchestratorResponse.body;

    // If orchestrator returned an error, pass it through
    if (orchestratorResponse.status >= 400) {
      res.status(orchestratorResponse.status).json(responseBody);
      return;
    }

    // Egress validation: validate AvaResult schema before returning
    // Per spec: "Validate AvaResult schema before returning"
    if (orchestratorResponse.status === 200 && typeof responseBody === 'object' && responseBody !== null) {
      const validation = validateAvaResult(responseBody);
      if (!validation.valid) {
        // Log the validation failure but still return the response
        // with a warning header (don't break the client)
        console.warn(
          `[EGRESS] AvaResult validation warning: ${validation.errors}`,
          { correlationId, suiteId: suiteId.substring(0, 8) },
        );
        res.setHeader('X-Aspire-Egress-Warning', 'AvaResult schema validation failed');
      }
    }

    res.status(orchestratorResponse.status).json(responseBody);
  } catch (err) {
    if (err instanceof OrchestratorClientError) {
      const statusMap: Record<string, number> = {
        TIMEOUT: 504,
        CONNECTION_REFUSED: 503,
        INVALID_RESPONSE: 502,
        UNKNOWN: 500,
      };

      res.status(statusMap[err.code] ?? 500).json({
        error: err.code === 'TIMEOUT' ? 'INTERNAL_ERROR' : 'INTERNAL_ERROR',
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
});
