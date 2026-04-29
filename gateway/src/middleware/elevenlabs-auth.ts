// [STATUS: v1+v2] — Validates shared tool secret on V1 ElevenLabs + V2 Anam tool webhook calls.
/**
 * Tool Auth Middleware — Shared Secret Verification (Law #3: Fail Closed)
 *
 * Validates requests from V1 ElevenLabs server tools AND V2 Anam persona tools
 * using a shared secret. Both call our `/v1/tools/*` endpoints with the secret
 * in the header. Header name varies by source (historical):
 *   - V1 ElevenLabs agents send `x-elevenlabs-secret`
 *   - V2 Anam personas send `x-aspire-tool-secret`
 * Both are validated against the same `ELEVENLABS_TOOL_SECRET` env var.
 *
 * Auth flow:
 * 1. Read either `x-elevenlabs-secret` OR `x-aspire-tool-secret` header (whichever present)
 * 2. Constant-time compare against ELEVENLABS_TOOL_SECRET env var
 * 3. Extract suite_id from request body (passed via dynamic variables) or x-suite-id header
 * 4. Set req.auth with extracted context for downstream handlers
 *
 * Law #3: Missing secret or mismatch = 401 (fail-closed)
 * Law #6: suite_id validated server-side
 * Law #9: Never log the secret value
 */

import type { Request, Response, NextFunction } from 'express';

/**
 * Middleware that validates ElevenLabs server tool requests.
 * Fail-closed: missing env var, missing header, or mismatch = 401.
 */
export function elevenlabsToolAuthMiddleware(req: Request, res: Response, next: NextFunction): void {
  const toolSecret = process.env.ELEVENLABS_TOOL_SECRET;

  // Law #3: Fail-closed — secret not configured = deny
  if (!toolSecret) {
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'ElevenLabs tool secret not configured. Fail-closed per Law #3.',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  // Accept either header — V1 ElevenLabs agents send `x-elevenlabs-secret`,
  // V2 Anam personas send `x-aspire-tool-secret`. Both authenticate against
  // the same ELEVENLABS_TOOL_SECRET env var.
  const elevenlabsHeader = req.headers['x-elevenlabs-secret'];
  const anamHeader = req.headers['x-aspire-tool-secret'];
  const providedSecret =
    (typeof elevenlabsHeader === 'string' && elevenlabsHeader) ||
    (typeof anamHeader === 'string' && anamHeader) ||
    '';

  // Law #3: Fail-closed — missing header = deny
  if (!providedSecret) {
    res.status(401).json({
      error: 'AUTH_FAILED',
      message: 'Missing x-elevenlabs-secret or x-aspire-tool-secret header',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  // Constant-time comparison to prevent timing attacks
  if (providedSecret.length !== toolSecret.length || !timingSafeCompare(providedSecret, toolSecret)) {
    res.status(401).json({
      error: 'AUTH_FAILED',
      message: 'Invalid ElevenLabs tool secret',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  // Extract suite_id from body OR header (THREAT-001 fix: support both patterns)
  // ElevenLabs server tools may pass suite_id in the request body (dynamic variables)
  // or in the x-suite-id header (configured in tool headers). Check both, body first.
  const bodySuiteId = req.body?.suite_id;
  const headerSuiteId = req.headers['x-suite-id'];
  const rawSuiteId = (typeof bodySuiteId === 'string' && bodySuiteId) ||
                     (typeof headerSuiteId === 'string' && headerSuiteId) || '';

  // UUID format validation (THREAT-007 fix)
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!rawSuiteId || !UUID_RE.test(rawSuiteId)) {
    res.status(400).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing or invalid suite_id. Must be a valid UUID. Law #6: tenant isolation.',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  const rawUserId = req.body?.user_id || req.headers['x-user-id'] || 'elevenlabs-agent';

  // Set auth context for downstream handlers
  req.auth = {
    suiteId: rawSuiteId,
    officeId: rawSuiteId, // ElevenLabs tools operate at suite level
    actorId: typeof rawUserId === 'string' ? rawUserId : 'elevenlabs-agent',
    actorType: 'agent',
  };

  next();
}

/**
 * Timing-safe string comparison to prevent timing attacks.
 * Returns false if lengths differ (caller checks length first).
 */
function timingSafeCompare(a: string, b: string): boolean {
  const bufA = Buffer.from(a, 'utf-8');
  const bufB = Buffer.from(b, 'utf-8');
  try {
    // crypto.timingSafeEqual throws if lengths differ
    return require('crypto').timingSafeEqual(bufA, bufB);
  } catch {
    return false;
  }
}
