export type ApiErrorBody = {
  code: string;
  message: string;
  correlation_id?: string;
  details?: unknown;
};

function baseHeaders(correlationId?: string): Headers {
  const h = new Headers();
  h.set("Content-Type", "application/json");
  if (correlationId) h.set("X-Correlation-Id", correlationId);
  return h;
}

export function jsonError(
  status: number,
  code: string,
  message: string,
  correlationId?: string,
  details?: unknown,
): Response {
  const body: ApiErrorBody = { code, message };
  if (correlationId) body.correlation_id = correlationId;
  if (details !== undefined) body.details = details;
  return new Response(JSON.stringify(body), {
    status,
    headers: baseHeaders(correlationId),
  });
}

export function jsonOk(data: unknown, correlationId?: string, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: baseHeaders(correlationId),
  });
}
