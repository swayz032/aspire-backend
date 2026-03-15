export type IncidentSeverity = 'sev1' | 'sev2' | 'sev3' | 'sev4';

export interface GatewayIncidentReport {
  title: string;
  severity: IncidentSeverity;
  correlationId: string;
  suiteId?: string | null;
  component: string;
  fingerprint: string;
  actorId?: string;
  errorCode?: string | null;
  statusCode?: number | null;
  message?: string | null;
  metadata?: Record<string, unknown>;
}

function trimEnv(value: string | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

function getIncidentReporterSecret(): string {
  return (
    trimEnv(process.env.ASPIRE_ADMIN_INCIDENT_S2S_SECRET) ||
    trimEnv(process.env.S2S_HMAC_SECRET_ACTIVE) ||
    trimEnv(process.env.DOMAIN_RAIL_HMAC_SECRET) ||
    trimEnv(process.env.S2S_HMAC_SECRET)
  );
}

function resolveOrchestratorBaseUrl(): string {
  return trimEnv(process.env.ORCHESTRATOR_URL) || 'http://localhost:8000';
}

export async function reportGatewayIncident(incident: GatewayIncidentReport): Promise<boolean> {
  const secret = getIncidentReporterSecret();
  if (!secret) return false;

  const orchestratorBase = resolveOrchestratorBaseUrl();
  const url = `${orchestratorBase.replace(/\/+$/, '')}/admin/ops/incidents/report`;
  const traceId = incident.correlationId;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${secret}`,
        'X-Correlation-Id': incident.correlationId,
        'X-Trace-Id': traceId,
        'X-Actor-Id': incident.actorId || 'aspire-gateway',
      },
      body: JSON.stringify({
        title: incident.title,
        severity: incident.severity,
        source: 'aspire_gateway',
        component: incident.component,
        state: 'open',
        suite_id: incident.suiteId || null,
        correlation_id: incident.correlationId,
        trace_id: traceId,
        fingerprint: incident.fingerprint,
        error_code: incident.errorCode || null,
        status_code: typeof incident.statusCode === 'number' ? incident.statusCode : null,
        message: incident.message || null,
        evidence_pack: {
          source: 'aspire_gateway',
          component: incident.component,
          metadata: incident.metadata || {},
        },
      }),
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}
