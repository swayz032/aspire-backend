/**
 * Registry Route Handler — GET /v1/registry/*
 *
 * Control Plane Registry discovery endpoints.
 * Provides capability discovery so clients can see available
 * skill packs, agents, tools, and risk tiers before submitting intents.
 *
 * All endpoints are read-only. No state changes, no receipts.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';

export const registryRouter = Router();

function getSingleParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? '';
  return value ?? '';
}

/**
 * GET /v1/registry/capabilities — List all registered capabilities.
 *
 * Optional query params:
 *   category: filter by category (channel, finance, legal, etc.)
 *   risk_tier: filter by risk tier (green, yellow, red)
 *   status: filter by status (registered, active, suspended)
 */
registryRouter.get('/capabilities', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const queryParams: Record<string, string> = {};
  if (req.query.category) queryParams.category = String(req.query.category);
  if (req.query.risk_tier) queryParams.risk_tier = String(req.query.risk_tier);
  if (req.query.status) queryParams.status = String(req.query.status);

  // Validate risk_tier if provided
  if (queryParams.risk_tier && !['green', 'yellow', 'red'].includes(queryParams.risk_tier)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid risk_tier: ${queryParams.risk_tier}. Must be green, yellow, or red.`,
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: '/v1/registry/capabilities',
      method: 'GET',
      queryParams,
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

/**
 * GET /v1/registry/skill-packs/:packId — Get a specific skill pack manifest.
 */
registryRouter.get('/skill-packs/:packId', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const packId = getSingleParam(req.params.packId);

  if (!packId || !/^[a-z][a-z0-9_]*$/.test(packId)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Invalid pack ID format',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: `/v1/registry/skill-packs/${packId}`,
      method: 'GET',
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(response.status).json(response.body);
  } catch (err) {
    if (err instanceof OrchestratorClientError) {
      res.status(503).json({
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

/**
 * GET /v1/registry/route/:actionType — Route an action to its skill pack.
 */
registryRouter.get('/route/:actionType(*)', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const actionType = getSingleParam(req.params.actionType);

  if (!actionType || !/^[a-z][a-z0-9.]*[a-z0-9]$/i.test(actionType)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid action_type format: ${actionType}`,
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const response = await proxyToOrchestrator({
      path: `/v1/registry/route/${actionType}`,
      method: 'GET',
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(response.status).json(response.body);
  } catch (err) {
    if (err instanceof OrchestratorClientError) {
      res.status(503).json({
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
