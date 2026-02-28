import { requireUser } from "../_shared/auth.ts";
import { getCorrelationId } from "../_shared/correlation.ts";
import { jsonError, jsonOk } from "../_shared/errors.ts";

function extractApprovalId(reqUrl: string): string | null {
  const url = new URL(reqUrl);

  // Backwards compat: approval_id in query
  const q = url.searchParams.get("approval_id");
  if (q) return q;

  const parts = url.pathname.split("/").filter(Boolean);
  // Support both:
  // - /functions/v1/approval-events/v1/approvals/{id}/events
  // - /approval-events/v1/approvals/{id}/events
  // - /functions/v1/approval-events/{id}
  const fnIdx = parts.lastIndexOf("approval-events");
  if (fnIdx === -1) return null;

  const tail = parts.slice(fnIdx + 1);
  if (tail.length === 0) return null;

  // /v1/approvals/{id}/events
  if (tail.length >= 4 && tail[0] === "v1" && tail[1] === "approvals" && tail[3] === "events") {
    return tail[2];
  }

  // /{id} (or /{id}/events)
  if (tail.length >= 1) return tail[0];

  return null;
}

function toApiEvent(row: any) {
  return {
    id: row.id,
    approval_id: row.approval_id,
    actor: String(row.actor_user_id ?? "system"),
    event_type: row.event_type,
    reason_code: row.reason_code ?? null,
    draft_artifact: row.draft_artifact ?? null,
    final_artifact: row.final_artifact ?? null,
    diff: row.diff ?? null,
    created_at: row.created_at,
  };
}

Deno.serve(async (req) => {
  const correlationId = getCorrelationId(req);

  try {
    const approvalId = extractApprovalId(req.url);
    if (!approvalId) {
      return jsonError(400, "BAD_REQUEST", "approval_id is required", correlationId);
    }

    const { supabase, userId } = await requireUser(req);

    // Resolve tenant context via approval_requests. RLS should prevent cross-tenant leakage.
    const { data: approvalRow, error: approvalErr } = await supabase
      .from("approval_requests")
      .select("tenant_id")
      .eq("approval_id", approvalId)
      .maybeSingle();

    if (approvalErr) {
      return jsonError(500, "DB_ERROR", approvalErr.message, correlationId);
    }
    if (!approvalRow?.tenant_id) {
      // Either not found, or caller is not authorized by RLS.
      return jsonError(404, "NOT_FOUND", "Approval not found", correlationId);
    }

    const tenantId = approvalRow.tenant_id as string;

    if (req.method === "GET") {
      const { data, error } = await supabase
        .from("approval_events")
        .select("*")
        .eq("tenant_id", tenantId)
        .eq("approval_id", approvalId)
        .order("created_at", { ascending: true });

      if (error) return jsonError(500, "DB_ERROR", error.message, correlationId);
      return jsonOk({ items: (data ?? []).map(toApiEvent) }, correlationId);
    }

    if (req.method === "POST") {
      const body = await req.json().catch(() => null);
      if (!body || typeof body !== "object") {
        return jsonError(400, "BAD_REQUEST", "Invalid JSON body", correlationId);
      }

      const eventType = (body as any).event_type;
      if (!eventType) return jsonError(400, "BAD_REQUEST", "event_type required", correlationId);

      const payload = {
        id: crypto.randomUUID(),
        tenant_id: tenantId,
        approval_id: approvalId,
        actor_user_id: userId,
        event_type: eventType,
        reason_code: (body as any).reason_code ?? null,
        draft_artifact: (body as any).draft_artifact ?? null,
        final_artifact: (body as any).final_artifact ?? null,
        diff: (body as any).diff ?? null,
      };

      const { data, error } = await supabase.from("approval_events").insert(payload).select("*").single();
      if (error) return jsonError(500, "DB_ERROR", error.message, correlationId);
      return jsonOk(toApiEvent(data), correlationId, 201);
    }

    return jsonError(405, "METHOD_NOT_ALLOWED", "Method not allowed", correlationId);
  } catch (e) {
    const msg = String((e as any)?.message ?? e);
    const status = msg.includes("Unauthenticated") ? 401 : msg.includes("Authorization") ? 401 : 500;
    return jsonError(status, "INTERNAL_ERROR", msg, correlationId);
  }
});
