/**
 * ElevenLabs Sessions Router — SECURITY-SCOPED
 *
 * Only exposes /signed-url for JWT-authenticated session creation.
 * Does NOT expose /draft, /approve, /execute, /context, /search.
 *
 * Fix for THREAT-002: The full elevenlabsToolsRouter was previously
 * mounted at /v1/sessions, exposing RED-tier execution endpoints
 * to any JWT-authenticated user without the tool-secret gate.
 *
 * Law #3: Fail-closed — only the signed-url route is registered.
 * Law #9: ElevenLabs API key stays server-side.
 */

import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { reportGatewayIncident } from '../services/incident-reporter.js';
import { logger } from '../services/logger.js';

export const elevenlabsSessionsRouter = Router();

const ELEVENLABS_API_KEY = process.env.ELEVENLABS_API_KEY || '';

const AGENT_ID_MAP: Record<string, string> = {
  ava: process.env.ELEVENLABS_AGENT_AVA || '',
  eli: process.env.ELEVENLABS_AGENT_ELI || '',
  finn: process.env.ELEVENLABS_AGENT_FINN || '',
  nora: process.env.ELEVENLABS_AGENT_NORA || '',
  sarah: process.env.ELEVENLABS_AGENT_SARAH || '',
};

const VALID_AGENTS = new Set(Object.keys(AGENT_ID_MAP));

/**
 * POST /v1/sessions/signed-url
 * Generate an ElevenLabs signed URL for starting an agent session.
 * Requires JWT auth — suite_id derived from token (Law #6).
 */
elevenlabsSessionsRouter.post('/signed-url', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;
  const { suiteId, officeId, actorId } = req.auth;

  const agent = (req.body.agent || '').toLowerCase().trim();
  if (!VALID_AGENTS.has(agent)) {
    res.status(400).json({
      error: 'INVALID_AGENT',
      message: `Invalid agent: ${agent}. Valid: ${[...VALID_AGENTS].join(', ')}`,
      correlation_id: correlationId,
    });
    return;
  }

  const agentId = AGENT_ID_MAP[agent];
  if (!agentId) {
    res.status(503).json({
      error: 'AGENT_NOT_CONFIGURED',
      message: `Agent ${agent} is not configured. Set ELEVENLABS_AGENT_${agent.toUpperCase()} env var.`,
      correlation_id: correlationId,
    });
    return;
  }

  if (!ELEVENLABS_API_KEY) {
    res.status(503).json({
      error: 'ELEVENLABS_NOT_CONFIGURED',
      message: 'ElevenLabs API key not configured.',
      correlation_id: correlationId,
    });
    return;
  }

  try {
    // Fetch user profile from orchestrator for dynamic variables
    let dynamicVariables: Record<string, string> = {
      suite_id: suiteId,
      user_id: actorId,
      office_id: officeId,
    };

    try {
      const profileResp = await proxyToOrchestrator({
        path: '/v1/user-profile',
        method: 'GET',
        correlationId,
        suiteId,
        officeId,
        actorId,
      });
      if (profileResp.status === 200 && typeof profileResp.body === 'object' && profileResp.body) {
        const profile = profileResp.body as Record<string, unknown>;
        dynamicVariables = {
          ...dynamicVariables,
          salutation: String(profile.salutation || 'Mr.'),
          last_name: String(profile.last_name || ''),
          owner_name: String(profile.owner_name || ''),
          business_name: String(profile.business_name || ''),
          industry: String(profile.industry || ''),
          time_of_day: getTimeOfDay(),
        };
      }
    } catch {
      // Non-fatal: session proceeds with minimal dynamic variables
      // SECURITY (THREAT-006 fix): suite_id is ALWAYS from JWT, never client-supplied
      logger.warn('Profile fetch failed for signed URL — proceeding with minimal vars', { suiteId, correlationId });
    }

    // Get signed URL from ElevenLabs
    const url = `https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id=${agentId}`;
    const elResp = await fetch(url, {
      headers: { 'xi-api-key': ELEVENLABS_API_KEY },
    });

    if (!elResp.ok) {
      const errorBody = await elResp.text().catch(() => 'unknown');
      // SECURITY (THREAT-003 fix): truncate error body to prevent key fingerprint leakage
      logger.error('ElevenLabs signed URL request failed', {
        status: elResp.status,
        errorBody: errorBody.slice(0, 200).replace(/xi-[a-zA-Z0-9]+/g, '[REDACTED]'),
        correlationId,
      });
      res.status(502).json({
        error: 'ELEVENLABS_ERROR',
        message: 'Failed to get signed URL from ElevenLabs',
        correlation_id: correlationId,
      });
      return;
    }

    const elData = await elResp.json();

    res.json({
      signed_url: elData.signed_url,
      agent_id: agentId,
      dynamic_variables: dynamicVariables,
      correlation_id: correlationId,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    logger.error('Signed URL generation failed', { error: message, correlationId });
    void reportGatewayIncident({
      title: 'ElevenLabs signed URL generation failed',
      severity: 'sev2',
      correlationId,
      suiteId,
      component: '/v1/sessions/signed-url',
      fingerprint: `gateway:sessions:signed-url:${suiteId}`,
      actorId,
      errorCode: 'SIGNED_URL_FAILED',
      statusCode: 500,
      message: message.slice(0, 200),
    });
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'Failed to generate session',
      correlation_id: correlationId,
    });
  }
});

function getTimeOfDay(): string {
  const hour = new Date().getUTCHours();
  if (hour < 12) return 'morning';
  if (hour < 17) return 'afternoon';
  return 'evening';
}
