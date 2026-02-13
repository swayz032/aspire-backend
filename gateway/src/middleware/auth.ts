/**
 * Auth Middleware — JWT Extraction + suite_id Derivation (Law #6: Tenant Isolation)
 *
 * CRITICAL: suite_id is derived from auth context, NEVER from request body.
 * Per architecture.md: "The orchestrator derives the authoritative suite_id from JWT,
 * NOT from this payload."
 *
 * Two modes:
 * 1. PRODUCTION (default): Verify JWT with Supabase JWT secret, extract user claims
 * 2. DEV/TEST (GATEWAY_AUTH_MODE=dev): Accept x-suite-id, x-office-id, x-actor-id headers
 *
 * Fail-closed: missing or invalid auth → 401 (Law #3)
 */

import type { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import type { AspireAuthContext } from '../types/express.js';

export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  const authMode = process.env.GATEWAY_AUTH_MODE ?? 'production';
  if (authMode === 'dev') {
    handleDevAuth(req, res, next);
  } else {
    handleJwtAuth(req, res, next);
  }
}

/**
 * Dev/test mode: extract auth context from headers.
 * Only available when GATEWAY_AUTH_MODE=dev.
 */
function handleDevAuth(req: Request, res: Response, next: NextFunction): void {
  const suiteId = req.headers['x-suite-id'];
  const officeId = req.headers['x-office-id'];
  const actorId = req.headers['x-actor-id'];

  if (typeof suiteId !== 'string' || suiteId.length === 0) {
    res.status(401).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing x-suite-id header (dev auth mode)',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  if (typeof officeId !== 'string' || officeId.length === 0) {
    res.status(401).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing x-office-id header (dev auth mode)',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  const actorType = (req.headers['x-actor-type'] as string) ?? 'user';
  const validActorTypes = ['user', 'system', 'agent', 'scheduler'];
  if (!validActorTypes.includes(actorType)) {
    res.status(401).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Invalid x-actor-type: ${actorType}`,
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  req.auth = {
    suiteId: suiteId as string,
    officeId: officeId as string,
    actorId: typeof actorId === 'string' ? actorId : 'dev-user',
    actorType: actorType as AspireAuthContext['actorType'],
  };

  next();
}

/**
 * Production mode: verify JWT from Authorization header.
 * Fail-closed if JWT secret is not configured (Law #3).
 */
function handleJwtAuth(req: Request, res: Response, next: NextFunction): void {
  const JWT_SECRET = process.env.SUPABASE_JWT_SECRET ?? '';
  if (!JWT_SECRET) {
    res.status(500).json({
      error: 'INTERNAL_ERROR',
      message: 'JWT secret not configured. Fail-closed per Law #3.',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    res.status(401).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: 'Missing or malformed Authorization header. Expected: Bearer <JWT>',
      correlation_id: req.correlationId ?? 'unknown',
    });
    return;
  }

  const token = authHeader.slice(7);

  try {
    const decoded = jwt.verify(token, JWT_SECRET, {
      algorithms: ['HS256'],
    }) as jwt.JwtPayload;

    // Supabase JWT claims structure:
    // sub = user UUID, app_metadata.suite_id, app_metadata.office_id
    const suiteId = decoded.app_metadata?.suite_id ?? decoded.suite_id;
    const officeId = decoded.app_metadata?.office_id ?? decoded.office_id;
    const actorId = decoded.sub;

    if (!suiteId || !officeId || !actorId) {
      res.status(401).json({
        error: 'TENANT_ISOLATION_VIOLATION',
        message: 'JWT missing required claims: suite_id, office_id, or sub',
        correlation_id: req.correlationId ?? 'unknown',
      });
      return;
    }

    req.auth = {
      suiteId,
      officeId,
      actorId,
      actorType: (decoded.actor_type as AspireAuthContext['actorType']) ?? 'user',
    };

    next();
  } catch (err) {
    const message = err instanceof Error ? err.message : 'JWT verification failed';
    res.status(401).json({
      error: 'SCHEMA_VALIDATION_FAILED',
      message: `Authentication failed: ${message}`,
      correlation_id: req.correlationId ?? 'unknown',
    });
  }
}
