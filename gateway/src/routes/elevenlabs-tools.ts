/**
 * ElevenLabs Agent Tool Endpoints — Pass 1A Hybrid Architecture
 *
 * Thin REST endpoints that ElevenLabs agents call as server tools.
 * Each endpoint proxies to the existing LangGraph orchestrator.
 *
 * Endpoints:
 * - POST /signed-url — Generate signed URL for agent session (JWT auth)
 * - POST /context    — Get user context (GREEN tier, tool auth)
 * - POST /search     — Search across domains (GREEN tier, tool auth)
 * - POST /draft      — Create draft action (YELLOW tier, tool auth)
 * - POST /approve    — Approve pending action (YELLOW/RED tier, tool auth)
 * - POST /execute    — Execute approved action (RED tier, tool auth)
 *
 * Law #1: Single Brain — all decisions proxy to LangGraph orchestrator
 * Law #3: Fail-closed — missing params/auth = deny
 * Law #6: Tenant isolation — suite_id from auth context, never client
 * Law #9: Never log secrets
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { reportGatewayIncident } from '../services/incident-reporter.js';
import { logger } from '../services/logger.js';

export const elevenlabsToolsRouter = Router();

/** Valid agent names for signed URL generation */
const VALID_AGENTS = ['ava', 'eli', 'finn', 'nora', 'sarah'] as const;
type AgentName = typeof VALID_AGENTS[number];

/** Map agent name to env var containing ElevenLabs agent ID */
const AGENT_ENV_MAP: Record<AgentName, string> = {
  ava: 'ELEVENLABS_AGENT_AVA',
  eli: 'ELEVENLABS_AGENT_ELI',
  finn: 'ELEVENLABS_AGENT_FINN',
  nora: 'ELEVENLABS_AGENT_NORA',
  sarah: 'ELEVENLABS_AGENT_SARAH',
};

/** Valid search domains */
const VALID_DOMAINS = ['calendar', 'contacts', 'emails', 'invoices'] as const;

// ---------------------------------------------------------------------------
// POST /signed-url — Generate ElevenLabs signed URL for agent session
// Auth: JWT (authMiddleware applied at mount point in server.ts)
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/signed-url', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  // Validate agent param
  const { agent } = req.body ?? {};
  if (typeof agent !== 'string' || !VALID_AGENTS.includes(agent as AgentName)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid agent. Must be one of: ${VALID_AGENTS.join(', ')}`,
      correlation_id: correlationId,
    });
    return;
  }

  const agentName = agent as AgentName;
  const agentId = process.env[AGENT_ENV_MAP[agentName]];

  // Law #3: Fail-closed — agent ID not configured
  if (!agentId) {
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: `ElevenLabs agent ID not configured for ${agentName}. Fail-closed per Law #3.`,
      correlation_id: correlationId,
    });
    return;
  }

  const apiKey = process.env.ELEVENLABS_API_KEY;
  if (!apiKey) {
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'ElevenLabs API key not configured. Fail-closed per Law #3.',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    // Step 1: Fetch user profile from orchestrator for dynamic variables
    const profileResponse = await proxyToOrchestrator({
      path: '/v1/user-profile',
      method: 'GET',
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    const userProfile = (profileResponse.status === 200 && typeof profileResponse.body === 'object')
      ? profileResponse.body as Record<string, unknown>
      : {};

    // Step 2: Get signed URL from ElevenLabs
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000);

    const signedUrlResponse = await fetch(
      `https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id=${encodeURIComponent(agentId)}`,
      {
        method: 'GET',
        headers: { 'xi-api-key': apiKey },
        signal: controller.signal,
      },
    );
    clearTimeout(timeoutId);

    if (!signedUrlResponse.ok) {
      await signedUrlResponse.text().catch(() => {}); // drain response body
      logger.error('ElevenLabs signed URL request failed', {
        correlation_id: correlationId,
        suite_id: suiteId.substring(0, 8),
        status: signedUrlResponse.status,
      });
      res.status(502).json({
        error: 'UPSTREAM_ERROR',
        message: 'Failed to obtain signed URL from ElevenLabs',
        correlation_id: correlationId,
      });
      return;
    }

    const signedUrlData = await signedUrlResponse.json() as Record<string, unknown>;

    res.status(200).json({
      signed_url: signedUrlData.signed_url,
      agent_id: agentId,
      dynamic_variables: {
        suite_id: suiteId,
        user_id: actorId,
        salutation: userProfile.salutation ?? '',
        first_name: userProfile.first_name ?? '',
        last_name: userProfile.last_name ?? '',
        company_name: userProfile.company_name ?? '',
        timezone: userProfile.timezone ?? 'UTC',
      },
      correlation_id: correlationId,
    });
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/signed-url');
  }
});

// ---------------------------------------------------------------------------
// POST /context — Get user context (GREEN tier)
// Auth: ElevenLabs tool secret (elevenlabsToolAuthMiddleware)
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/context', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { query } = req.body ?? {};

  if (typeof query !== 'string' || query.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: query',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'context_lookup',
        suite_id: suiteId,
        user_id: actorId,
        query,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/context');
  }
});

// ---------------------------------------------------------------------------
// POST /search — Search across domains (GREEN tier)
// Auth: ElevenLabs tool secret
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/search', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { query, domain } = req.body ?? {};

  if (typeof query !== 'string' || query.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: query',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof domain !== 'string' || !VALID_DOMAINS.includes(domain as typeof VALID_DOMAINS[number])) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid domain. Must be one of: ${VALID_DOMAINS.join(', ')}`,
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'search',
        suite_id: suiteId,
        user_id: actorId,
        query,
        domain,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/search');
  }
});

// ---------------------------------------------------------------------------
// POST /draft — Create draft action (YELLOW tier)
// Auth: ElevenLabs tool secret
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/draft', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { action, params } = req.body ?? {};

  if (typeof action !== 'string' || action.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: action',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof params !== 'object' || params === null) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: params (must be an object)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'draft_action',
        suite_id: suiteId,
        user_id: actorId,
        action,
        params,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    const responseBody = orchestratorResponse.body as Record<string, unknown> | null;

    // YELLOW tier: always signal that confirmation is needed
    res.status(orchestratorResponse.status).json({
      ...responseBody,
      requires_confirmation: true,
      correlation_id: correlationId,
    });
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/draft');
  }
});

// ---------------------------------------------------------------------------
// POST /approve — Approve pending action (YELLOW/RED tier)
// Auth: ElevenLabs tool secret
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/approve', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { draft_id, action } = req.body ?? {};

  if (typeof draft_id !== 'string' || draft_id.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: draft_id',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof action !== 'string' || action.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: action',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'approve_action',
        suite_id: suiteId,
        user_id: actorId,
        draft_id,
        action,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/approve');
  }
});

// ---------------------------------------------------------------------------
// POST /execute — Execute approved action (RED tier)
// Auth: ElevenLabs tool secret
// ---------------------------------------------------------------------------
elevenlabsToolsRouter.post('/execute', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { capability_token, action, params } = req.body ?? {};

  if (typeof capability_token !== 'string' || capability_token.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: capability_token (Law #5)',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof action !== 'string' || action.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: action',
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof params !== 'object' || params === null) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: params (must be an object)',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'execute_action',
        suite_id: suiteId,
        user_id: actorId,
        capability_token,
        action,
        params,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/execute');
  }
});

// ---------------------------------------------------------------------------
// POST /invoke — Invoke internal agent (GREEN/YELLOW tier)
// Auth: ElevenLabs tool secret
// Ava uses this to route tasks to Quinn, Clara, Adam, Tec, etc.
// ---------------------------------------------------------------------------
const VALID_AGENTS_TO_INVOKE = ['quinn', 'clara', 'adam', 'tec', 'eli', 'nora', 'finn', 'sarah'] as const;

elevenlabsToolsRouter.post('/invoke', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;
  const { agent, task, params } = req.body ?? {};

  if (typeof agent !== 'string' || !VALID_AGENTS_TO_INVOKE.includes(agent as typeof VALID_AGENTS_TO_INVOKE[number])) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid agent. Must be one of: ${VALID_AGENTS_TO_INVOKE.join(', ')}`,
      correlation_id: correlationId,
    });
    return;
  }

  if (typeof task !== 'string' || task.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: task',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/intents',
      method: 'POST',
      body: {
        intent: 'invoke_agent',
        suite_id: suiteId,
        user_id: actorId,
        agent,
        task,
        params: typeof params === 'object' && params !== null ? params : {},
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId,
      actorId,
    });

    res.status(orchestratorResponse.status).json(orchestratorResponse.body);
  } catch (err) {
    handleToolError(err, res, correlationId, suiteId, actorId, '/invoke');
  }
});

// ---------------------------------------------------------------------------
// Shared error handler — follows intents.ts pattern
// ---------------------------------------------------------------------------
function handleToolError(
  err: unknown,
  res: Response,
  correlationId: string,
  suiteId: string,
  actorId: string,
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

    void reportGatewayIncident({
      title: `ElevenLabs tool endpoint ${endpoint} orchestrator failure`,
      severity: mappedStatus >= 503 ? 'sev1' : 'sev2',
      correlationId,
      suiteId,
      component: `/v1/tools${endpoint}`,
      fingerprint: `gateway:elevenlabs-tools:${endpoint}:${suiteId}:${actorId}:${err.code.toLowerCase()}`,
      actorId,
      errorCode: `ORCHESTRATOR_${err.code}`,
      statusCode: mappedStatus,
      message: err.message,
    });

    res.status(mappedStatus).json({
      error: 'INTERNAL_ERROR',
      message: err.message,
      correlation_id: correlationId,
    });
    return;
  }

  void reportGatewayIncident({
    title: `ElevenLabs tool endpoint ${endpoint} unexpected failure`,
    severity: 'sev2',
    correlationId,
    suiteId,
    component: `/v1/tools${endpoint}`,
    fingerprint: `gateway:elevenlabs-tools:${endpoint}:${suiteId}:${actorId}:unexpected`,
    actorId,
    errorCode: 'INTERNAL_ERROR',
    statusCode: 500,
    message: err instanceof Error ? err.message : 'Unknown error',
  });

  res.status(500).json({
    error: 'INTERNAL_ERROR',
    message: err instanceof Error ? err.message : 'Unknown error',
    correlation_id: correlationId,
  });
}
