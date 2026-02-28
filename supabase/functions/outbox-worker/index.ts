import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { getCorrelationId } from "../_shared/correlation.ts";
import { jsonError, jsonOk } from "../_shared/errors.ts";

function env(name: string): string {
  const v = Deno.env.get(name);
  if (!v) throw new Error(`Missing env var: ${name}`);
  return v;
}

function requireWorkerSecret(req: Request): boolean {
  const expected = Deno.env.get("TRUST_SPINE_WORKER_SECRET");
  // If not configured, fail closed.
  if (!expected || expected.trim() === "") return false;
  const got = req.headers.get("X-Worker-Secret") ?? "";
  return got === expected;
}

/**
 * Outbox worker stub.
 *
 * Expected flow:
 * 1) internal worker calls claim_outbox_jobs(suite_id, limit, worker_id)
 * 2) execute provider call(s)
 * 3) write provider_call_log + execution_receipts
 * 4) mark outbox job SUCCEEDED/FAILED/DEAD
 */
Deno.serve(async (req) => {
  const correlationId = getCorrelationId(req);

  try {
    if (req.method !== "POST") return jsonError(405, "METHOD_NOT_ALLOWED", "Method not allowed", correlationId);

    if (!requireWorkerSecret(req)) {
      return jsonError(401, "UNAUTHORIZED", "Missing/invalid worker secret", correlationId);
    }

    const url = env("SUPABASE_URL");
    const serviceKey = env("SUPABASE_SERVICE_ROLE_KEY");
    const supabase = createClient(url, serviceKey);

    const body = await req.json().catch(() => null);
    if (!body || typeof body !== "object") {
      return jsonError(400, "BAD_REQUEST", "Invalid JSON body", correlationId);
    }

    let suiteId = String((body as any).suite_id ?? "");
    const legacyTenantId = String((body as any).tenant_id ?? "");

    const limit = Number((body as any).limit ?? 10);
    const workerId = String((body as any).worker_id ?? "edge-worker");

    if (!suiteId && legacyTenantId) {
      const { data, error } = await supabase
        .schema("app")
        .from("suites")
        .select("suite_id")
        .eq("tenant_id", legacyTenantId)
        .maybeSingle();
      if (error) return jsonError(500, "DB_ERROR", error.message, correlationId);
      suiteId = String((data as any)?.suite_id ?? "");
    }

    if (!suiteId) return jsonError(400, "BAD_REQUEST", "suite_id required", correlationId);

    const { data: jobs, error } = await supabase.rpc("claim_outbox_jobs", {
      p_suite_id: suiteId,
      p_limit: limit,
      p_worker_id: workerId,
    });
    if (error) return jsonError(500, "DB_ERROR", error.message, correlationId);

    // Stub: Claude should implement provider execution per action_type.
    return jsonOk({ suite_id: suiteId, claimed: jobs ?? [] }, correlationId);
  } catch (e) {
    return jsonError(500, "INTERNAL_ERROR", String((e as any)?.message ?? e), correlationId);
  }
});
