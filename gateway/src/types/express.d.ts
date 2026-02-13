/**
 * Express Request augmentation for Aspire Gateway.
 *
 * Adds auth context and correlation ID to the request object.
 * These are populated by middleware before route handlers execute.
 */

export interface AspireAuthContext {
  suiteId: string;
  officeId: string;
  actorId: string;
  actorType: 'user' | 'system' | 'agent' | 'scheduler';
}

declare global {
  namespace Express {
    interface Request {
      /** Correlation ID for distributed tracing (set by correlation-id middleware) */
      correlationId: string;
      /** Auth context derived from JWT or dev headers (set by auth middleware) */
      auth: AspireAuthContext;
    }
  }
}
