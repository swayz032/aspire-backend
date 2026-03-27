/**
 * ElevenLabs Webhook Endpoint — Post-Call Transcript Ingestion
 *
 * Receives post-call webhooks from ElevenLabs with full transcripts,
 * conversation metadata, and analysis. Verifies HMAC signature before
 * processing.
 *
 * Law #2: Receipt produced for every transcript ingestion
 * Law #3: Fail-closed — invalid/missing signature = 401
 * Law #9: Never log secrets or PII from transcripts
 */

import crypto from 'crypto';
import { Router } from 'express';
import type { Request, Response } from 'express';
import {
  proxyToOrchestrator,
  OrchestratorClientError,
} from '../services/orchestrator-client.js';
import { reportGatewayIncident } from '../services/incident-reporter.js';
import { logger } from '../services/logger.js';

export const elevenlabsWebhooksRouter = Router();

// ---------------------------------------------------------------------------
// POST /transcripts — Receive post-call webhooks from ElevenLabs
// Auth: HMAC signature verification via `elevenlabs-signature` header
// ---------------------------------------------------------------------------
elevenlabsWebhooksRouter.post('/transcripts', async (req: Request, res: Response) => {
  const correlationId = req.correlationId;

  // Step 1: Verify webhook signature (Law #3: fail-closed)
  const webhookSecret = process.env.ELEVENLABS_WEBHOOK_SECRET;
  if (!webhookSecret) {
    logger.error('ElevenLabs webhook secret not configured', { correlation_id: correlationId });
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'Webhook secret not configured. Fail-closed per Law #3.',
      correlation_id: correlationId,
    });
    return;
  }

  const signatureHeader = req.headers['elevenlabs-signature'] as string | undefined;
  if (!signatureHeader) {
    res.status(401).json({
      error: 'WEBHOOK_SIGNATURE_INVALID',
      message: 'Missing elevenlabs-signature header',
      correlation_id: correlationId,
    });
    return;
  }

  // Compute HMAC-SHA256 over the raw body
  const rawBody = Buffer.isBuffer(req.body)
    ? req.body
    : Buffer.from(JSON.stringify(req.body), 'utf-8');

  const computedSignature = crypto
    .createHmac('sha256', webhookSecret)
    .update(rawBody)
    .digest('hex');

  // Timing-safe comparison
  if (
    computedSignature.length !== signatureHeader.length ||
    !crypto.timingSafeEqual(Buffer.from(computedSignature, 'utf-8'), Buffer.from(signatureHeader, 'utf-8'))
  ) {
    res.status(401).json({
      error: 'WEBHOOK_SIGNATURE_INVALID',
      message: 'Signature mismatch',
      correlation_id: correlationId,
    });
    return;
  }

  // Parse body if it arrived as raw Buffer
  let body: Record<string, unknown>;
  if (Buffer.isBuffer(req.body)) {
    try {
      body = JSON.parse(req.body.toString('utf-8')) as Record<string, unknown>;
    } catch {
      res.status(400).json({
        error: 'WEBHOOK_BODY_INVALID',
        message: 'Request body is not valid JSON',
        correlation_id: correlationId,
      });
      return;
    }
  } else {
    body = req.body as Record<string, unknown>;
  }

  // Step 2: Validate required fields
  const conversationId = body.conversation_id;
  if (typeof conversationId !== 'string' || conversationId.length === 0) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing required field: conversation_id',
      correlation_id: correlationId,
    });
    return;
  }

  // Extract suite_id from metadata if present (for tenant routing)
  const metadata = (body.metadata ?? {}) as Record<string, unknown>;
  const suiteId = typeof metadata.suite_id === 'string' ? metadata.suite_id : 'system';
  const userId = typeof metadata.user_id === 'string' ? metadata.user_id : 'webhook_ingress';

  // Step 3: Forward to orchestrator for storage + receipt generation
  try {
    const orchestratorResponse = await proxyToOrchestrator({
      path: '/v1/webhooks/elevenlabs/transcripts',
      method: 'POST',
      body: {
        conversation_id: conversationId,
        transcript: body.transcript,
        analysis: body.analysis,
        metadata,
        correlation_id: correlationId,
      },
      correlationId,
      suiteId,
      officeId: suiteId,
      actorId: userId,
    });

    if (orchestratorResponse.status >= 500) {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook orchestrator failure',
        severity: 'sev2',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:http_${orchestratorResponse.status}`,
        actorId: userId,
        errorCode: `ORCHESTRATOR_HTTP_${orchestratorResponse.status}`,
        statusCode: orchestratorResponse.status,
        message: typeof orchestratorResponse.body === 'object' && orchestratorResponse.body
          ? JSON.stringify(orchestratorResponse.body).slice(0, 300)
          : String(orchestratorResponse.body).slice(0, 300),
      });
    }

    // Return 200 OK to ElevenLabs regardless (prevent retries on our processing errors)
    // The orchestrator handles receipt generation
    res.status(200).json({
      status: 'received',
      conversation_id: conversationId,
      correlation_id: correlationId,
    });
  } catch (err) {
    if (err instanceof OrchestratorClientError) {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook orchestrator unreachable',
        severity: 'sev1',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:${err.code.toLowerCase()}`,
        actorId: userId,
        errorCode: `ORCHESTRATOR_${err.code}`,
        statusCode: 502,
        message: err.message,
      });
    } else {
      void reportGatewayIncident({
        title: 'ElevenLabs transcript webhook unexpected failure',
        severity: 'sev2',
        correlationId,
        suiteId,
        component: '/v1/webhooks/elevenlabs/transcripts',
        fingerprint: `gateway:elevenlabs-webhook:transcripts:${suiteId}:unexpected`,
        actorId: userId,
        errorCode: 'INTERNAL_ERROR',
        statusCode: 500,
        message: err instanceof Error ? err.message : 'Unknown error',
      });
    }

    // Still return 200 to ElevenLabs to prevent retry storms
    // Incident is reported; orchestrator will reconcile later
    res.status(200).json({
      status: 'received',
      conversation_id: conversationId,
      correlation_id: correlationId,
    });
  }
});
