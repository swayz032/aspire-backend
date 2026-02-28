export function getCorrelationId(req: Request): string {
  const existing = req.headers.get("X-Correlation-Id");
  return existing && existing.trim().length > 0 ? existing.trim() : crypto.randomUUID();
}
