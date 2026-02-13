/**
 * Correlation ID Middleware — Distributed Tracing (Gate 2: Observability)
 *
 * Ensures every request has a correlation ID for full trace linkage.
 * - If X-Correlation-Id header present: propagate it
 * - If absent: generate UUID v4
 * - Attach to req.correlationId
 * - Set on response header
 */

import { v4 as uuidv4 } from 'uuid';
import type { Request, Response, NextFunction } from 'express';

const HEADER_NAME = 'x-correlation-id';

export function correlationIdMiddleware(req: Request, res: Response, next: NextFunction): void {
  const existing = req.headers[HEADER_NAME];
  const correlationId = typeof existing === 'string' && existing.length > 0
    ? existing
    : uuidv4();

  req.correlationId = correlationId;
  res.setHeader('X-Correlation-Id', correlationId);
  next();
}
