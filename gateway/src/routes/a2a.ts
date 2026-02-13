/**
 * A2A Route Handler — POST /v1/a2a/*
 *
 * Agent-to-Agent task routing endpoints.
 * Enables the orchestrator (Law #1) to dispatch tasks to skill pack agents,
 * and agents to claim/complete/fail tasks.
 *
 * All state changes produce receipts (Law #2).
 * All operations are tenant-scoped via suite_id (Law #6).
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';

export const a2aRouter = Router();

// Shared error handler for orchestrator proxy failures
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

/**
 * POST /v1/a2a/dispatch — Dispatch a task to a skill pack agent.
 *
 * Body: { task_type, assigned_to_agent, payload, priority?, idempotency_key? }
 * suite_id/office_id are injected from auth context (Law #6).
 */
a2aRouter.post('/dispatch', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { task_type, assigned_to_agent } = req.body ?? {};

  if (typeof task_type !== 'string' || task_type.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'task_type is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof assigned_to_agent !== 'string' || assigned_to_agent.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'assigned_to_agent is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/a2a/dispatch',
      method: 'POST',
      body: {
        ...req.body,
        suite_id: suiteId,
        office_id: officeId,
        correlation_id: correlationId,
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

/**
 * POST /v1/a2a/claim — Claim available tasks for an agent.
 *
 * Body: { agent_id, task_types?, max_tasks?, lease_seconds? }
 * suite_id is injected from auth context (Law #6).
 */
a2aRouter.post('/claim', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { agent_id } = req.body ?? {};

  if (typeof agent_id !== 'string' || agent_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'agent_id is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/a2a/claim',
      method: 'POST',
      body: {
        ...req.body,
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

/**
 * POST /v1/a2a/complete — Mark a claimed task as completed.
 *
 * Body: { task_id, agent_id, result? }
 * suite_id is injected from auth context (Law #6).
 */
a2aRouter.post('/complete', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { task_id, agent_id } = req.body ?? {};

  if (typeof task_id !== 'string' || task_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'task_id is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof agent_id !== 'string' || agent_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'agent_id is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/a2a/complete',
      method: 'POST',
      body: {
        ...req.body,
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

/**
 * POST /v1/a2a/fail — Mark a claimed task as failed.
 *
 * Body: { task_id, agent_id, error }
 * suite_id is injected from auth context (Law #6).
 */
a2aRouter.post('/fail', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const { task_id, agent_id, error } = req.body ?? {};

  if (typeof task_id !== 'string' || task_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'task_id is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof agent_id !== 'string' || agent_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'agent_id is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof error !== 'string' || error.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'error is required (string)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/a2a/fail',
      method: 'POST',
      body: {
        ...req.body,
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

/**
 * GET /v1/a2a/tasks — List tasks for a suite.
 *
 * Query params: status?, assigned_to_agent?, limit?
 * suite_id is injected from auth context (Law #6).
 */
a2aRouter.get('/tasks', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const queryParams: Record<string, string> = {
    suite_id: suiteId,
  };
  if (req.query.status) queryParams.status = String(req.query.status);
  if (req.query.assigned_to_agent) queryParams.assigned_to_agent = String(req.query.assigned_to_agent);
  if (req.query.limit) queryParams.limit = String(req.query.limit);

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/a2a/tasks',
      method: 'GET',
      queryParams,
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
