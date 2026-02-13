/**
 * Orchestrator HTTP Client — Gateway-to-Orchestrator Bridge (W5-04)
 *
 * HTTP client that proxies requests from the TypeScript Gateway to the
 * Python LangGraph Orchestrator running on localhost:8000.
 *
 * Responsibilities:
 * - Forward requests with auth context headers
 * - Propagate correlation IDs for distributed tracing (Gate 2)
 * - Enforce 30s timeout (Gate 3: Reliability)
 * - Structured error handling with fail-closed semantics (Law #3)
 */

const ORCHESTRATOR_BASE_URL = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8000';
const ORCHESTRATOR_TIMEOUT_MS = parseInt(process.env.ORCHESTRATOR_TIMEOUT_MS ?? '30000', 10);

export interface OrchestratorProxyOptions {
  path: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  correlationId: string;
  suiteId: string;
  officeId: string;
  actorId: string;
  queryParams?: Record<string, string>;
}

export interface OrchestratorResponse {
  status: number;
  body: unknown;
  headers: Record<string, string>;
}

export class OrchestratorClientError extends Error {
  constructor(
    message: string,
    public readonly code: 'TIMEOUT' | 'CONNECTION_REFUSED' | 'INVALID_RESPONSE' | 'UNKNOWN',
    public readonly correlationId: string,
  ) {
    super(message);
    this.name = 'OrchestratorClientError';
  }
}

/**
 * Proxy a request to the Python orchestrator.
 *
 * Propagates auth context via headers (x-suite-id, x-office-id, x-actor-id).
 * The orchestrator uses these headers as the authoritative auth context.
 */
export async function proxyToOrchestrator(options: OrchestratorProxyOptions): Promise<OrchestratorResponse> {
  const { path, method, body, correlationId, suiteId, officeId, actorId, queryParams } = options;

  let url = `${ORCHESTRATOR_BASE_URL}${path}`;
  if (queryParams && Object.keys(queryParams).length > 0) {
    const params = new URLSearchParams(queryParams);
    url += `?${params.toString()}`;
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Correlation-Id': correlationId,
    'X-Suite-Id': suiteId,
    'X-Office-Id': officeId,
    'X-Actor-Id': actorId,
  };

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), ORCHESTRATOR_TIMEOUT_MS);

  try {
    const fetchOptions: RequestInit = {
      method,
      headers,
      signal: controller.signal,
    };

    if (body !== undefined && method !== 'GET') {
      fetchOptions.body = JSON.stringify(body);
    }

    const response = await fetch(url, fetchOptions);
    clearTimeout(timeoutId);

    let responseBody: unknown;
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      responseBody = await response.json();
    } else {
      responseBody = await response.text();
    }

    const responseHeaders: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      responseHeaders[key] = value;
    });

    return {
      status: response.status,
      body: responseBody,
      headers: responseHeaders,
    };
  } catch (err) {
    clearTimeout(timeoutId);

    if (err instanceof Error) {
      if (err.name === 'AbortError') {
        throw new OrchestratorClientError(
          `Orchestrator request timed out after ${ORCHESTRATOR_TIMEOUT_MS}ms (Gate 3 timeout enforcement)`,
          'TIMEOUT',
          correlationId,
        );
      }

      // Connection refused (orchestrator is down)
      if ('cause' in err && (err.cause as NodeJS.ErrnoException)?.code === 'ECONNREFUSED') {
        throw new OrchestratorClientError(
          'Orchestrator service unavailable (connection refused)',
          'CONNECTION_REFUSED',
          correlationId,
        );
      }

      // Fetch-level errors (ECONNREFUSED comes as TypeError in Node fetch)
      if (err.message.includes('ECONNREFUSED') || err.message.includes('fetch failed')) {
        throw new OrchestratorClientError(
          `Orchestrator service unavailable: ${err.message}`,
          'CONNECTION_REFUSED',
          correlationId,
        );
      }
    }

    throw new OrchestratorClientError(
      `Unexpected orchestrator error: ${err instanceof Error ? err.message : String(err)}`,
      'UNKNOWN',
      correlationId,
    );
  }
}

/**
 * Health check the orchestrator.
 * Returns true if orchestrator responds to /healthz with 200.
 */
export async function checkOrchestratorHealth(): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);

    const response = await fetch(`${ORCHESTRATOR_BASE_URL}/healthz`, {
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    return response.ok;
  } catch {
    return false;
  }
}
