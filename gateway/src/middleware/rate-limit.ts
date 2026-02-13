/**
 * Rate Limiting Middleware — Abuse Prevention (Gate 5: Security)
 *
 * Per-suite_id rate limiting with configurable windows.
 * Uses express-rate-limit with suite_id as the key generator.
 *
 * Default: 100 requests/minute per suite.
 * Policy evaluation: 200 requests/minute (lightweight, no execution).
 */

import rateLimit from 'express-rate-limit';
import type { Request } from 'express';

/**
 * Key generator: rate limit per suite_id (tenant isolation).
 * Falls back to IP if auth hasn't been processed yet.
 */
function suiteKeyGenerator(req: Request): string {
  return req.auth?.suiteId ?? req.ip ?? 'anonymous';
}

/**
 * Standard rate limiter for state-changing endpoints.
 * 100 requests per minute per suite.
 */
export const standardRateLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 100,
  keyGenerator: suiteKeyGenerator,
  standardHeaders: true,
  legacyHeaders: false,
  message: {
    error: 'POLICY_DENIED',
    message: 'Rate limit exceeded. Maximum 100 requests per minute per suite.',
    correlation_id: 'rate_limited',
  },
});

/**
 * Elevated rate limiter for read-only/lightweight endpoints.
 * 200 requests per minute per suite.
 */
export const elevatedRateLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 200,
  keyGenerator: suiteKeyGenerator,
  standardHeaders: true,
  legacyHeaders: false,
  message: {
    error: 'POLICY_DENIED',
    message: 'Rate limit exceeded. Maximum 200 requests per minute per suite.',
    correlation_id: 'rate_limited',
  },
});
