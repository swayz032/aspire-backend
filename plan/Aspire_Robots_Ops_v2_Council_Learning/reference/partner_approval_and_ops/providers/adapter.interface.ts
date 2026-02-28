/**
 * Aspire Provider Adapter Contract (Required)
 * Enforces: idempotency, redaction, simulation (replay), stable error taxonomy.
 */

export type PreflightResult =
  | { ok: true; warnings?: string[] }
  | { ok: false; errors: { code: string; message: string; field?: string }[] };

export type ErrorClass = "retryable" | "nonretryable" | "fatal";

export interface ProviderAdapter<P = unknown, R = unknown> {
  preflight(payload: P): Promise<PreflightResult>;
  execute(payload: P, idempotencyKey: string): Promise<R>;
  simulate(payload: P): Promise<R>;
  classifyError(err: unknown): { class: ErrorClass; code: string; message: string; retryAfterMs?: number };
  redact(data: unknown): unknown;
}
