/**
 * Policy Route Handler — POST /v1/policy/evaluate
 *
 * Law #4: Risk Tiers. This endpoint allows the UI to evaluate a policy
 * for a given action_type without executing. Used to show risk tier
 * indicators, approval requirements, and capability scope before the
 * user commits to an action.
 *
 * This is a read-only operation that does NOT produce execution receipts.
 * It produces a policy_evaluation receipt for audit purposes.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';

export const policyRouter = Router();

/**
 * POST /v1/policy/evaluate — Evaluate policy for an action type.
 *
 * Request body:
 *   action_type: string — The action to evaluate (e.g., "invoice.create")
 *
 * Response:
 *   risk_tier: "green" | "yellow" | "red"
 *   allowed: boolean
 *   approval_required: boolean
 *   presence_required: boolean
 *   tools: string[]
 *   capability_scope: string
 */
policyRouter.post('/evaluate', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { action_type } = req.body ?? {};
  if (typeof action_type !== 'string' || action_type.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Request body must include action_type (string)',
      correlation_id: correlationId,
    });
    return;
  }

  // Validate action_type format: only allow alphanumeric + dots
  if (!/^[a-z][a-z0-9.]*[a-z0-9]$/i.test(action_type)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid action_type format: ${action_type}. Expected: domain.action (e.g., invoice.create)`,
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/policy/evaluate',
      method: 'POST',
      body: {
        action_type,
        suite_id: suiteId,
        office_id: officeId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(response.status).json(response.body);
  } catch (err) {
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
});
