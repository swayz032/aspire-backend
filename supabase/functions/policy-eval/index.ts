// policy-eval: minimal wrapper around trust_policy_eval
// Option B: suite_id canonical, tenant_id legacy.
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { requireUser, requireSuiteMember, requireTenantMember } from "../_shared/auth.ts";
import { getCorrelationId, withCorrelationHeaders } from "../_shared/correlation.ts";
import { jsonError, jsonOk } from "../_shared/errors.ts";

serve(async (req) => {
  try {
    const correlationId = getCorrelationId(req);

    if (req.method !== "POST") {
      return withCorrelationHeaders(jsonError(405, "method_not_allowed", "Use POST"), correlationId);
    }

    const { user } = await requireUser(req);
    const body = await req.json();

    const suiteId = String(body?.suite_id ?? "");
    const legacyTenantId = String(body?.tenant_id ?? "");

    if (!suiteId && !legacyTenantId) {
      return withCorrelationHeaders(jsonError(400, "bad_request", "suite_id is required"), correlationId);
    }

    let tenantIdResolved = legacyTenantId;

    if (suiteId) {
      const { tenantId } = await requireSuiteMember(req, suiteId);
      tenantIdResolved = tenantId;
    } else {
      await requireTenantMember(req, legacyTenantId);
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;

    const supabase = createClient(supabaseUrl, anonKey, {
      global: { headers: { Authorization: req.headers.get("Authorization")! } },
    });

    const { data, error } = await supabase.rpc("trust_policy_eval", {
      p_tenant_id: tenantIdResolved,
      p_subject: body?.subject ?? { user_id: user.id },
      p_action: body?.action ?? {},
    });

    if (error) {
      return withCorrelationHeaders(jsonError(500, "rpc_error", error.message), correlationId);
    }

    return withCorrelationHeaders(
      jsonOk({ correlation_id: correlationId, suite_id: suiteId || null, tenant_id: tenantIdResolved, decision: data }),
      correlationId
    );
  } catch (e) {
    return jsonError(500, "internal_error", String((e as any)?.message ?? e));
  }
});
