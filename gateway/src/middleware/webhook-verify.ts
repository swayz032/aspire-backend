/**
 * Webhook Signature Verification Middleware — HMAC-SHA256
 *
 * Verifies incoming webhook payloads from external providers using
 * provider-specific signature headers and timing-safe comparison.
 *
 * Supported providers:
 *   - Stripe: `stripe-signature` header (HMAC-SHA256 with timestamp)
 *   - Gusto: `x-gusto-signature` header (HMAC-SHA256)
 *   - Plaid: `plaid-verification` header (HMAC-SHA256)
 *   - Generic: `x-webhook-signature` header (HMAC-SHA256)
 *
 * Law #3: Fail-closed — missing/invalid signature = 401
 * Law #2: Verification results produce receipts
 */

import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';

/**
 * Configuration for webhook verification per provider.
 */
interface WebhookProviderConfig {
  /** Environment variable name containing the webhook secret */
  secretEnvVar: string;
  /** Header name for the signature */
  signatureHeader: string;
  /** Optional: Extract signature from header value (e.g., Stripe's `t=...,v1=...`) */
  extractSignature?: (headerValue: string) => string | null;
  /** Optional: Construct the signed payload (e.g., Stripe prefixes with timestamp) */
  buildSignedPayload?: (rawBody: Buffer, headerValue: string) => Buffer;
}

const PROVIDER_CONFIGS: Record<string, WebhookProviderConfig> = {
  stripe: {
    secretEnvVar: 'STRIPE_WEBHOOK_SECRET',
    signatureHeader: 'stripe-signature',
    extractSignature: (header: string) => {
      // Stripe format: t=timestamp,v1=signature
      const match = header.match(/v1=([a-f0-9]+)/);
      return match ? match[1] : null;
    },
    buildSignedPayload: (rawBody: Buffer, header: string) => {
      // Stripe signs: `${timestamp}.${rawBody}`
      const match = header.match(/t=(\d+)/);
      const timestamp = match ? match[1] : '0';
      return Buffer.from(`${timestamp}.${rawBody.toString('utf-8')}`);
    },
  },
  gusto: {
    secretEnvVar: 'GUSTO_WEBHOOK_SECRET',
    signatureHeader: 'x-gusto-signature',
  },
  plaid: {
    secretEnvVar: 'PLAID_WEBHOOK_SECRET',
    signatureHeader: 'plaid-verification',
  },
  pandadoc: {
    secretEnvVar: 'PANDADOC_WEBHOOK_SECRET',
    signatureHeader: 'x-pandadoc-signature',
  },
  generic: {
    secretEnvVar: 'WEBHOOK_SECRET',
    signatureHeader: 'x-webhook-signature',
  },
};

/**
 * Compute HMAC-SHA256 signature for a payload.
 */
function computeHmac(secret: string, payload: Buffer): string {
  return crypto
    .createHmac('sha256', secret)
    .update(payload)
    .digest('hex');
}

/**
 * Timing-safe string comparison to prevent timing attacks.
 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  const bufA = Buffer.from(a, 'utf-8');
  const bufB = Buffer.from(b, 'utf-8');
  return crypto.timingSafeEqual(bufA, bufB);
}

/**
 * Verify a webhook signature for a given provider.
 *
 * @returns { valid: true } or { valid: false, reason: string }
 */
export function verifyWebhookSignature(
  provider: string,
  rawBody: Buffer,
  signatureHeader: string | undefined,
): { valid: boolean; reason?: string } {
  const config = PROVIDER_CONFIGS[provider];
  if (!config) {
    return { valid: false, reason: `Unknown webhook provider: ${provider}` };
  }

  // Law #3: Fail-closed — no secret configured = deny
  const secret = process.env[config.secretEnvVar];
  if (!secret) {
    return {
      valid: false,
      reason: `Webhook secret not configured: ${config.secretEnvVar}`,
    };
  }

  // Law #3: Fail-closed — no signature = deny
  if (!signatureHeader) {
    return {
      valid: false,
      reason: `Missing signature header: ${config.signatureHeader}`,
    };
  }

  // Extract the actual signature value (provider-specific parsing)
  let expectedSig: string;
  if (config.extractSignature) {
    const extracted = config.extractSignature(signatureHeader);
    if (!extracted) {
      return { valid: false, reason: 'Could not extract signature from header' };
    }
    expectedSig = extracted;
  } else {
    expectedSig = signatureHeader;
  }

  // Build the signed payload (provider-specific)
  const signedPayload = config.buildSignedPayload
    ? config.buildSignedPayload(rawBody, signatureHeader)
    : rawBody;

  // Compute and compare
  const computedSig = computeHmac(secret, signedPayload);

  if (!timingSafeEqual(computedSig, expectedSig)) {
    return { valid: false, reason: 'Signature mismatch' };
  }

  return { valid: true };
}

/**
 * Express middleware factory for webhook signature verification.
 *
 * Usage:
 *   app.post('/webhooks/stripe', webhookVerify('stripe'), stripeHandler);
 *   app.post('/webhooks/gusto', webhookVerify('gusto'), gustoHandler);
 *
 * IMPORTANT: This middleware requires raw body parsing. Ensure
 * `express.raw({ type: 'application/json' })` is used on webhook routes.
 */
export function webhookVerify(provider: string) {
  const config = PROVIDER_CONFIGS[provider];
  if (!config) {
    throw new Error(`Unknown webhook provider: ${provider}`);
  }

  return (req: Request, res: Response, next: NextFunction): void => {
    // Get raw body (requires express.raw() middleware on this route)
    const rawBody = Buffer.isBuffer(req.body)
      ? req.body
      : Buffer.from(JSON.stringify(req.body), 'utf-8');

    const signatureHeader = req.headers[config.signatureHeader] as string | undefined;

    const result = verifyWebhookSignature(provider, rawBody, signatureHeader);

    if (!result.valid) {
      res.status(401).json({
        error: 'WEBHOOK_SIGNATURE_INVALID',
        message: result.reason,
        provider,
      });
      return;
    }

    // Parse JSON body if it came in as raw Buffer
    if (Buffer.isBuffer(req.body)) {
      try {
        req.body = JSON.parse(req.body.toString('utf-8'));
      } catch {
        res.status(400).json({
          error: 'WEBHOOK_BODY_INVALID',
          message: 'Request body is not valid JSON',
          provider,
        });
        return;
      }
    }

    next();
  };
}

export default webhookVerify;
