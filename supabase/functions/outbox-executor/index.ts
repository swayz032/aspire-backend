// outbox-executor: minimal worker runner (scaffold)
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { jsonError, jsonOk } from "../_shared/errors.ts";

function requireWorkerSecret(req: Request) {
  const expected = Deno.env.get("TRUST_SPINE_WORKER_SECRET");
  if (!expected) throw new Error("TRUST_SPINE_WORKER_SECRET is not set");
  const got = req.headers.get("X-Worker-Secret") ?? "";
  if (got !== expected) throw new Error("unauthorized_worker");
}

serve(async (req) => {
  try {
    if (req.method !== "POST") return jsonError(405, "method_not_allowed", "Use POST");

    requireWorkerSecret(req);

    const body = await req.json();
    let suiteId = String(body?.suite_id ?? "");
    const legacyTenantId = String(body?.tenant_id ?? "");
    const limit = Number(body?.limit ?? 10);

    if (!suiteId && legacyTenantId) {
      // Backwards compat
      const supabaseUrl0 = Deno.env.get("SUPABASE_URL")!;
      const serviceKey0 = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
      const sb0 = createClient(supabaseUrl0, serviceKey0);
      const { data, error } = await sb0
        .schema("app")
        .from("suites")
        .select("suite_id")
        .eq("tenant_id", legacyTenantId)
        .maybeSingle();
      if (error) return jsonError(500, "db_error", error.message);
      suiteId = String((data as any)?.suite_id ?? "");
    }

    if (!suiteId) return jsonError(400, "bad_request", "suite_id is required");

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, serviceKey);

    const workerId = String(body?.worker_id ?? "outbox-executor");
    const { data: jobs, error: claimErr } = await supabase.rpc("claim_outbox_jobs", {
      p_suite_id: suiteId,
      p_limit: limit,
      p_worker_id: workerId,
    });

    if (claimErr) return jsonError(500, "claim_failed", claimErr.message);

    const results: Array<{ id: string; status: string; error?: string }> = [];

    for (const job of (jobs ?? [])) {
      try {
        // TODO: route to provider adapters based on action_type
        // Scaffold: mark succeeded immediately
        const { error: doneErr } = await supabase.rpc("complete_outbox_job", { p_job_id: (job as any).id });
        if (doneErr) throw new Error(doneErr.message);
        results.push({ id: (job as any).id, status: "SUCCEEDED" });
      } catch (e) {
        const msg = String((e as any)?.message ?? e);
        await supabase.rpc("fail_outbox_job", { p_job_id: (job as any).id, p_error: msg });
        results.push({ id: (job as any).id, status: "FAILED", error: msg });
      }
    }

    return jsonOk({ suite_id: suiteId, claimed: (jobs ?? []).length, results });
  } catch (e) {
    const msg = String((e as any)?.message ?? e);
    const code = msg === "unauthorized_worker" ? 401 : 500;
    return jsonError(code, "worker_error", msg);
  }
});
